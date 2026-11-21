# SPDX-License-Identifier: GPL-2.0-or-later


class ZrnError(Exception):
    """What went wrong (message, naming the file or value) and how to fix it (hint)."""

    def __init__(self, code, message, hint=None, exit_code=1):
        super().__init__(message)
        self.code, self.message, self.hint, self.exit_code = code, message, hint, exit_code
