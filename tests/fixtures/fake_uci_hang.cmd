@echo off
REM Cross-platform launcher shim for the hanging UCI engine test fixture.
python "%~dp0fake_uci_hang.py" %*
