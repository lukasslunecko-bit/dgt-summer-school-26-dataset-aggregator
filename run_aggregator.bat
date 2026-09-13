@echo off
setlocal
py -3 "%~dp0dgt_dataset_aggregator.py" %*
if errorlevel 9009 (
  echo Python 3 was not found. Install Python 3.10 or later from python.org.
  pause
)
