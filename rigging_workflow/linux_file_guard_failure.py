# SPDX-License-Identifier: GPL-3.0-or-later
"""Local exception contract for the vendored Blenderctl Linux read guard."""

class Failure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
