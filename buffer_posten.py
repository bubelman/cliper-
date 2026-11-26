"""Plant Clips über die Buffer-API ein (TikTok + YouTube Shorts).

Liest clips/buffer_plan.json (Liste von Einträgen):
  {"id": "...", "url": "<öffentlicher Video-Link>", "text": "...", "title": "...",
   "network": "tiktok" | "youtube", "due": "2026-11-25T18:47:00+01:00", "status": "offen"}
und schreibt status ("geplant" / "fehler"), buffer_id und fehler zurück.
Der API-Key kommt aus dem GitHub-Secret BUFFER_API_KEY.
"""
import json, os, sys, urllib.request
from pathlib import Path

PLAN = Path(__file__).parent / "clips" / "buffer_plan.json"
GEPOSTET = Path(__file__).parent / "clips" / "gepostet.json"
YT_GEPOSTET = Path(__file__).parent / "clips" / "youtube_gepostet.json"
PRO_TAG = {"tiktok": 3,    # Levin 25.11.2026: „mach mal auf 3 pro Tag runter“
           "youtube": 1}   # Levin 25.11.2026: „mach yt nur ein“
KEY = os.environ.get("BUFFER_API_KEY", "").strip()
API = "https://api.buffer.com"


def gql(query, variables=None):
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode()[:500]}"}]}


def typ(name):
    q = """query($n:String!){__type(name:$n){name kind inputFields{name type{name kind ofType{name kind ofType{name}}}}
           fields{name args{name type{name kind ofType{name kind}}} type{name kind ofType{name kind}}} enumValues{name}}}"""
    return (gql(q, {"n": name}).get("data") or {}).get("__type")


def schema_zeigen():
    for n in ["Query", "Mutation", "DeletePostInput", "CreatePostInput", "PostInputMetaData", "YoutubePostMetadataInput",
              "TikTokPostMetadataInput", "TiktokPostMetadataInput", "AssetsInput", "VideoAssetInput",
              "SchedulingType", "ShareMode", "ChannelsInput", "YoutubePrivacy", "PostType", "YoutubeLicense"]:
        t = typ(n)
        if t:
            print(f"--- {n}:", json.dumps(t, ensure_ascii=False)[:2500])


def kanaele():
    org = gql("{ account { organizations { id name } } }")
    print("Organisationen:", json.dumps(org, ensure_ascii=False)[:500])
    ids = [o["id"] for o in ((org.get("data") or {}).get("account") or {}).get("organizations", [])]
    alle = []
    for oid in ids:
        r = gql("query($i:ChannelsInput!){ channels(input:$i){ id service name } }", {"i": {"organizationId": oid}})
        print("Kanäle:", json.dumps(r, ensure_ascii=False)[:800])
        alle += ((r.get("data") or {}).get("channels") or [])
    return alle


def main():
    if not KEY:
        print("BUFFER_API_KEY fehlt (GitHub Secret)."); sys.exit(1)
    if "--schema" in sys.argv:
        schema_zeigen()
    plan = json.loads(PLAN.read_text(encoding="utf-8")) if PLAN.exists() else []
    gepostet = json.loads(GEPOSTET.read_text(encoding="utf-8")) if GEPOSTET.exists() else {}
    yt_gepostet = json.loads(YT_GEPOSTET.read_text(encoding="utf-8")) if YT_GEPOSTET.exists() else {}

    # Einträge mit status "loeschen": geplanten Post bei Buffer wieder entfernen,
    # Clip kommt zurück in den Puffer (aus gepostet.json raus).
    for p in plan:
        if p.get("status") != "loeschen":
            continue
        if not p.get("buffer_id"):
            p["status"] = "geloescht"
        else:
            r = gql("""mutation($in: DeletePostInput!){ deletePost(input:$in){ __typename
                         ... on MutationError { message } } }""", {"in": {"id": p["buffer_id"]}})
            erg = (r.get("data") or {}).get("deletePost")
            if erg and not erg.get("message"):
                p["status"] = "geloescht"
                print("gelöscht:", p["id"], p["due"])
            else:
                p["status"] = "fehler"
                p["fehler"] = ("Löschen: " + ((erg or {}).get("message") or json.dumps(r.get("errors"), ensure_ascii=False)))[:400]
                print("FEHLER beim Löschen:", p["id"], p["fehler"])
                mu = typ("Mutation") or {}
                print("Lösch-Mutationen:", [ (f["name"], f["args"]) for f in mu.get("fields") or [] if "delete" in f["name"].lower()])
                print("DeletePostInput:", typ("DeletePostInput"))
                continue
        (gepostet if p["network"] == "tiktok" else yt_gepostet).pop(p["url"], None)

    # Höchstens PRO_TAG Posts pro Netzwerk und Tag: Überzählige bleiben im Puffer.
    tage = {}
    for p in plan:
        if p.get("status") == "geplant":
            k = (p["network"], p["due"][:10])
            tage[k] = tage.get(k, 0) + 1
    for p in sorted([p for p in plan if p.get("status", "offen") == "offen"], key=lambda p: p["due"]):
        k = (p["network"], p["due"][:10])
        grenze = PRO_TAG.get(p["network"], 99)
        if tage.get(k, 0) >= grenze and not p.get("extra"):   # "extra": Levin will ausnahmsweise mehr
            p["status"] = "zurueck"
            p["fehler"] = f"schon {grenze} {p['network']}-Posts an diesem Tag - Clip bleibt im Puffer"
            (gepostet if p["network"] == "tiktok" else yt_gepostet).pop(p["url"], None)
            print("zurück in den Puffer:", p["id"], p["due"])
        else:
            tage[k] = tage.get(k, 0) + 1
    GEPOSTET.write_text(json.dumps(gepostet, ensure_ascii=False, indent=1), encoding="utf-8")
    YT_GEPOSTET.write_text(json.dumps(yt_gepostet, ensure_ascii=False, indent=1), encoding="utf-8")
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    # 26.11.: Status bei Buffer nachsehen (gesendet? Fehler?) für alles, was schon fällig war
    import datetime as dt
    jetzt = dt.datetime.now(dt.timezone.utc)
    for p in plan:
        if p.get("buffer_id") and p.get("status") == "geplant":
            try:
                faellig = dt.datetime.fromisoformat(p["due"]) <= jetzt
            except ValueError:
                faellig = False
            if not faellig or p.get("buffer_status") == "sent":
                continue
            r = gql("query($i:PostInput!){ post(input:$i){ id status error { message } externalLink } }",
                    {"i": {"id": p["buffer_id"]}})
            post = (r.get("data") or {}).get("post")
            if post:
                p["buffer_status"] = post.get("status")
                if post.get("error"):
                    p["buffer_fehler"] = str(post["error"])[:300]
                if post.get("externalLink"):
                    p["link"] = post["externalLink"]
            else:
                p["buffer_status"] = "unbekannt: " + json.dumps(r.get("errors"), ensure_ascii=False)[:250]
            print("Status:", p["id"], p.get("buffer_status"), p.get("buffer_fehler", ""))
    if "--status" in sys.argv and not any(p.get("buffer_status", "").startswith("unbekannt") is False for p in plan):
        print(typ("Post"))
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    offen = [p for p in plan if p.get("status", "offen") == "offen"]
    ch = kanaele()
    if not offen:
        print("Nichts offen."); return
    kanal = {}
    for c in ch:
        s = (c.get("service") or "").lower()
        kanal.setdefault("youtube" if "youtube" in s else "tiktok" if "tiktok" in s else s, c["id"])
    m = """mutation($in: CreatePostInput!){ createPost(input:$in){
             ... on PostActionSuccess { post { id dueAt } }
             ... on MutationError { message } } }"""
    for p in offen:
        cid = kanal.get(p["network"])
        if not cid:
            p["status"], p["fehler"] = "fehler", f"kein {p['network']}-Kanal in Buffer verbunden"
            continue
        eingabe = {"text": p.get("text", ""), "channelId": cid, "schedulingType": "automatic",
                   "mode": "customScheduled", "dueAt": p["due"], "needsApproval": False,
                   "assets": [{"video": {"url": p["url"]}}]}
        if p["network"] == "youtube":
            eingabe["metadata"] = {"youtube": {"title": p.get("title") or p.get("text", "")[:100],
                                               "privacy": "public", "madeForKids": False,
                                               "categoryId": "24", "type": "short"}}
        if p["network"] == "tiktok" and p.get("title"):
            eingabe["metadata"] = {"tiktok": {"title": p["title"]}}
        r = gql(m, {"in": eingabe})
        erg = ((r.get("data") or {}).get("createPost") or {})
        if erg.get("post"):
            p["status"], p["buffer_id"] = "geplant", erg["post"]["id"]
            p.pop("fehler", None)
            print("geplant:", p["id"], p["network"], p["due"])
        else:
            p["status"] = "fehler"
            p["fehler"] = (erg.get("message") or json.dumps(r.get("errors"), ensure_ascii=False))[:400]
            print("FEHLER:", p["id"], p["fehler"])
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
