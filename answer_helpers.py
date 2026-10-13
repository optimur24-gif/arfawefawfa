"""Source-faithful answer labels, including incomplete PDF rows."""
from __future__ import annotations


def answer_options(question):
    entries = question.get("source_options") if question.get("source_faithful") else None
    if not entries:
        entries = [{"label": key, "text": text} for key, text in question["options"].items()]
    labels = [str(entry["label"]).lower() for entry in entries]
    result = []
    for index, entry in enumerate(entries):
        label = str(entry["label"]).lower()
        letter = "абвгдежз"[int(label)-1] if label.isdigit() and 1 <= int(label) <= 8 else label
        option_id = letter if labels.count(label) == 1 else f"{letter}~{index}"
        result.append({"id": option_id, "letter": letter, "label": label,
                       "text": str(entry["text"]), "missing": False})
    # Keep the source body unchanged. The explicit empty button lets users choose
    # the PDF's literal key even when its option text was omitted in the PDF.
    present = {option["letter"] for option in result}
    for letter in question["correct"]:
        if letter not in present:
            result.append({"id": letter, "letter": letter, "label": letter,
                           "text": "В PDF текст варианта отсутствует", "missing": True})
    return result


def source_option_lines(question):
    if question.get("source_faithful") and question.get("source_options"):
        return [f"{entry['label']}) {entry['text']}".rstrip() for entry in question["source_options"]]
    return [f"{key}) {text}" for key, text in sorted(question["options"].items())]


def selected_letters(question, selected):
    mapping = {option["id"]: option["letter"] for option in answer_options(question)}
    return {mapping[key] for key in selected if key in mapping}


def is_correct_selection(question, selected):
    options = {option["id"] for option in answer_options(question)}
    return bool(selected) and set(selected) <= options and selected_letters(question, selected) == set(question["correct"])


def correct_answer_lines(question, letters=None):
    result = []
    options = answer_options(question)
    for letter in (question["correct"] if letters is None else sorted(letters)):
        matches = [option for option in options if option["letter"] == letter]
        for option in matches:
            text = option["text"] or "В PDF вариант оставлен пустым"
            label = option["label"]
            if label.isdigit():
                label += f" (ключ: {letter})"
            result.append(f"{label}) {text}")
    return result


def answer_breakdown(question, selected):
    chosen = selected_letters(question, selected)
    expected = set(question["correct"])
    return {
        "matched": sorted(chosen & expected),
        "missed": sorted(expected - chosen),
        "extra": sorted(chosen - expected),
    }


def breakdown_lines(question, selected):
    result = answer_breakdown(question, selected)
    return [
        "✅ Выбраны правильно: " + (", ".join(result["matched"]) or "—"),
        "⚠️ Пропущены: " + (", ".join(result["missed"]) or "—"),
        "❌ Лишние: " + (", ".join(result["extra"]) or "—"),
    ]
