"""Build the portable one-file bot and ZIP without private runtime data."""
import ast
import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODULES = ("answer_helpers", "features", "feature_ui", "latin_cards", "latin_ui")


def without_imports(source, modules):
    tree = ast.parse(source)
    remove = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            remove.update(range(node.lineno, node.end_lineno + 1))
    return "".join(line for index, line in enumerate(source.splitlines(keepends=True), 1) if index not in remove)


def build():
    bot = without_imports((ROOT / "bot.py").read_text(encoding="utf-8"), MODULES)
    marker = 'BASE_DIR = Path(__file__).resolve().parent'
    before, after = bot.split(marker, 1)
    bundled = "\n\n".join(
        without_imports((ROOT / f"{name}.py").read_text(encoding="utf-8"), {"__future__", *MODULES})
        for name in MODULES)
    standalone = before + "\n# Bundled source modules\n" + bundled + "\n\n" + marker + after
    raw = (ROOT / "questions.txt").read_text(encoding="utf-8")
    if "'''" in raw:
        raise ValueError("Use another quote style for the embedded JSON")
    standalone = standalone.replace('EMBEDDED_QUESTIONS_JSON = None  # EMBED_DATABASE_HERE',
                                    "EMBEDDED_QUESTIONS_JSON = r'''" + raw + "'''")
    latin_raw = (ROOT / "latin_data.json").read_text(encoding="utf-8")
    standalone = standalone.replace("EMBEDDED_LATIN_JSON = None  # EMBED_LATIN_HERE", "EMBEDDED_LATIN_JSON = " + repr(latin_raw))
    compile(standalone, "bot_single.py", "exec")
    (ROOT / "bot_single.py").write_text(standalone, encoding="utf-8")
    files = ["bot.py", "bot_single.py", "questions.txt", "requirements.txt", ".env.example",
             ".gitignore", "README.txt", "SOURCE_NOTES.txt",
             "topic_review.json", "Dockerfile", ".dockerignore", "run_cloud.sh", "build_release.py"]
    files += ["latin_data.json", "INSTALL_LATIN.txt", "README.md", "compose.yaml", ".github/workflows/check.yml", ".github/workflows/run-bot.yml"]
    files += [f"{name}.py" for name in MODULES]
    files += sorted(path.name for path in ROOT.glob("test_*.py"))
    token_pattern = re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{35}\b")
    archive = ROOT / "anatomy_telegram_bot.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name in files:
            data = (ROOT / name).read_bytes()
            if token_pattern.search(data):
                raise ValueError(f"Private token detected in {name}")
            package.writestr("anatomy_bot/" + name, data)
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
    print("Updated bot_single.py and anatomy_telegram_bot.zip; private .env and runtime are excluded.")


if __name__ == "__main__":
    build()
