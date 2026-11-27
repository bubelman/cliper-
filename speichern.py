"""Speichert das Ergebnis eines Clip-Laufs, auch wenn inzwischen ein anderer Lauf
dieselben Dateien geändert hat (25.11.: sonst gingen Vorschau und Transkripte verloren).
JSON-Listen/Wörterbücher werden zusammengeführt, das Log wird angehängt."""
import json, subprocess
from pathlib import Path

DATEIEN = ["clips/tiktok_links.json", "clips/vorschau.json", "arbeit/erledigte_videos.json",
           "arbeit/stand.json", "arbeit/automatik_log.txt"]


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


meine = {f: Path(f).read_text(encoding="utf-8") for f in DATEIEN if Path(f).exists()}
auftrag_weg = not Path("arbeit/auftrag.json").exists()   # auto.py löscht den Auftrag nach dem Lauf
basis = git("rev-parse", "HEAD").stdout.strip()
git("fetch", "-q", "origin", "main")
git("reset", "-q", "--hard", "origin/main")

for f, text in meine.items():
    alt_basis = git("show", f"{basis}:{f}").stdout
    jetzt = Path(f).read_text(encoding="utf-8") if Path(f).exists() else ""
    if f.endswith(".txt"):
        neu = text[len(alt_basis):] if text.startswith(alt_basis) else text
        Path(f).write_text(jetzt + neu, encoding="utf-8")
    elif f.endswith("stand.json"):
        Path(f).write_text(text, encoding="utf-8")
    else:
        a, b = json.loads(jetzt or "null"), json.loads(text)
        if isinstance(b, dict):
            a = a if isinstance(a, dict) else {}
            a.update(b)
        elif isinstance(b, list):
            a = (a if isinstance(a, list) else []) + [x for x in b if x not in (a or [])]
        Path(f).write_text(json.dumps(a, indent=1, ensure_ascii=False), encoding="utf-8")
    git("add", "-f", f)

# 27.11.: Nach dem reset war der Auftrag wieder da und lief bei jedem Lauf weiter - jetzt wirklich löschen
if auftrag_weg and Path("arbeit/auftrag.json").exists():
    Path("arbeit/auftrag.json").unlink()
git("add", "-A", "arbeit/auftrag.json")
if git("diff", "--cached", "--quiet").returncode:
    import time
    git("commit", "-q", "-m", "Clips vom " + time.strftime("%d.%m.%Y"))
    for _ in range(3):
        if git("push", "-q").returncode == 0:
            print("gespeichert"); break
        git("pull", "-q", "--rebase")
