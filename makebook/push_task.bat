@echo off
rem ShogiHome などから棋譜をタスクとして積む
rem   push_task.bat "startpos moves 7g7f 3c3d ..."
cd /d "%~dp0"
py -3.12 push_task.py --kifu %1 --origin ShogiHome
