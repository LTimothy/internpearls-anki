"""Pure logic for AI card generation: no aqt/anki imports.

Prompt assembly, model-output parsing/validation, mechanical checks, the local
GUID namespace, and usage arithmetic. Everything here is unit-testable with
plain pytest; subprocess and Qt live in ai_cli.py and ai_dialog.py.
"""
import binascii
import os

GUID_PREFIX = "iplocal-"
GENERATED_TAG_LEAF = "Generated"
GENERATED_DECK_LEAF = "Generated"


def generated_guid():
    return GUID_PREFIX + binascii.hexlify(os.urandom(10)).decode()


def is_generated_guid(guid):
    return isinstance(guid, str) and guid.startswith(GUID_PREFIX)
