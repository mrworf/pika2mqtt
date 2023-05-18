#!/usr/bin/env python3

import http.server
import socketserver
import urllib.request
import shutil

PORT = 8000

class OurHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
                # print("Path: " + self.path)
                url = f'http://localhost{self.path}'
                self.send_response(200)
                self.end_headers()

                with urllib.request.urlopen(url) as response:
                        self.wfile.write(response.read())

Handler = OurHandler

with socketserver.TCPServer(("", PORT), Handler, False) as httpd:
        httpd.allow_reuse_address = True
        httpd.server_bind()
        httpd.server_activate()
        httpd.serve_forever()

