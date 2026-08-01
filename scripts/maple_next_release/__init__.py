"""Release/launch helpers for Issue #31 Lane A (Windows field-use foundation).

Pure-Python helpers only. Importable on any OS so tests can run on Linux CI;
Windows-only behavior (subprocess launch, real filesystem roots such as
LOCALAPPDATA) is exercised behind explicit environment/argument injection so
it stays testable without a Windows host.
"""

from __future__ import annotations
