"""Local desktop launcher. Starts the paper dashboard in the user's browser."""
import argparse
from pathlib import Path
import sys
import threading
import webbrowser


def main():
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10 veya üzeri gerekli; Python 3.12 önerilir.')
    parser=argparse.ArgumentParser(description='Spot Lab deneme panelini başlatır.')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--db',default=str(Path(__file__).resolve().parent/'.data'/'paper.sqlite3'))
    args=parser.parse_args()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('Europe/Istanbul')
    except Exception:
        raise SystemExit('Saat dilimi paketi gerekli: python -m pip install -r requirements.txt')
    if not args.no_browser:
        timer=threading.Timer(2,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}/'))
        timer.daemon=True
        timer.start()
    from app import main as serve
    sys.argv=[sys.argv[0],'--host','127.0.0.1','--port',str(args.port),'--db',args.db]
    serve()


if __name__=='__main__':
    main()
