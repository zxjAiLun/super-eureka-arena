@echo off
REM Cross-platform launcher shim for the fake UCI engine test fixture.
REM On Windows a .cmd file is needed so subprocess can execute it directly.
python "%~dp0fake_uci_engine.py" %*
