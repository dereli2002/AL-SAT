"""Run with: python3 app.py --host 127.0.0.1 --port 8080."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.service import BotService

BASE = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, service, **kwargs):
        self.service = service
        super().__init__(*args,directory=str(BASE/'static'),**kwargs)

    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Cache-Control','no-store')
        super().end_headers()

    def respond(self, payload, code=200):
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/state':
            self.respond(self.service.state())
        elif path=='/api/symbols':
            self.respond(self.service.symbols())
        elif path=='/api/trends':
            try:
                query=parse_qs(urlparse(self.path).query)
                symbols=query.get('symbols',[None])[0]
                self.respond(self.service.trends(symbols.split(',') if symbols else None))
            except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
                self.respond({'error':str(exc)},400)
        elif path=='/api/chart':
            try:
                query=parse_qs(urlparse(self.path).query)
                self.respond(self.service.chart(query.get('symbol',[None])[0],query.get('interval',[None])[0]))
            except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
                self.respond({'error':str(exc)},400)
            except Exception as exc:
                self.respond({'error':f'{type(exc).__name__}: {exc}'},502)
        elif path=='/api/health':
            state=self.service.state()
            self.respond(dict(ok=True,mode='paper',market_ready=state['status']['fresh'],
                              data_source=state['market']['source']))
        elif path.startswith('/api/'):
            self.respond({'error':'Bulunamadı.'},404)
        elif path in ('/','/index.html','/app.js','/style.css',
                      '/asset-picker.js','/asset-picker.css','/market-chart.js','/market-chart.css',
                      '/positions.html','/positions.js','/positions.css'):
            super().do_GET()
        else:
            self.send_error(404)

    def mutate(self, method):
        # Same-origin JSON requests; do not allow cross-site form submissions.
        origin=self.headers.get('Origin')
        if origin and origin not in ('http://'+self.headers.get('Host',''), 'https://'+self.headers.get('Host','')):
            return self.respond({'error':'Kaynak izinli değil.'},403)
        if not self.headers.get('Content-Type','').startswith('application/json'):
            return self.respond({'error':'JSON gerekli.'},415)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=16384:
                raise ValueError('İstek boyutu geçersiz.')
            payload=json.loads(self.rfile.read(size))
            if not isinstance(payload,dict):
                raise ValueError('JSON nesnesi gerekli.')
            path=urlparse(self.path).path
            if method=='PATCH' and path=='/api/config':
                result=self.service.configure(payload)
            elif method=='POST' and path=='/api/control':
                result=self.service.control(payload.get('action'),payload.get('symbol'),payload.get('amount'),
                                            payload.get('expected_emergency_revision'))
            elif method=='POST' and path=='/api/backtest':
                result=self.service.test_history(payload)
            else:
                return self.respond({'error':'Bulunamadı.'},404)
            self.respond(result)
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            self.respond({'error':str(exc)},400)
        except Exception as exc:
            self.respond({'error':f'{type(exc).__name__}: {exc}'},502)

    def do_POST(self):
        self.mutate('POST')

    def do_PATCH(self):
        self.mutate('PATCH')


def main():
    parser=argparse.ArgumentParser(description='Binance Spot deneme botu — gerçek emir göndermez.')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--db',default=str(BASE/'.data'/'paper.sqlite3'))
    args=parser.parse_args()
    service=BotService(args.db)
    server=ThreadingHTTPServer((args.host,args.port),partial(Handler,service=service))
    service.start_worker()
    print(f'Paper bot listening on {args.host}:{args.port}; no real orders.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__=='__main__':
    main()
