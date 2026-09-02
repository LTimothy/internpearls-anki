#!/usr/bin/env python3
# tests/fake_help_cli.py -- pretend CLI whose --help output is controlled by env
# vars, for supports_flag's word-boundary and subcommand-probe tests.
#   `<this> --help`            prints $FAKE_HELP_TOP
#   `<this> <subcmd> --help`   prints $FAKE_HELP_SUB, only if argv[1] == $FAKE_HELP_SUBCOMMAND
import os
import sys

args = sys.argv[1:]
if args == ["--help"]:
    print(os.environ.get("FAKE_HELP_TOP", ""))
elif (len(args) == 2 and args[1] == "--help"
      and args[0] == os.environ.get("FAKE_HELP_SUBCOMMAND")):
    print(os.environ.get("FAKE_HELP_SUB", ""))
sys.exit(0)
