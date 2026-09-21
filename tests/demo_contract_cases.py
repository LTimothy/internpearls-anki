"""Shared action cases for the mock and real-Qt demo contract tests."""


def contract_action_table(ids):
    return [
        {"type": "activate", "id": ids["button"]},
        {"type": "toggle", "id": ids["radio_b"], "checked": True},
        {"type": "select-option", "id": ids["combo"],
         "option_id": "option-second"},
        {"type": "edit-text", "id": ids["line"], "value": "x",
         "selection_start": 1, "selection_end": 1, "composing": False},
        {"type": "finish-edit", "id": ids["line"]},
        {"type": "activate-link", "id": ids["link"],
         "action_id": "details"},
        {"type": "scroll", "id": ids["scroll"], "offset": 7},
        {"type": "key", "id": ids["dialog"], "key": "Escape",
         "modifiers": []},
    ]
