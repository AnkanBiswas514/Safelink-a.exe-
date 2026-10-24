import http.server
import socketserver
import json
from urllib.parse import parse_qs, urlparse

PORT = 3000

class SOSHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # Allow cross-origin requests from the browser canvas
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"<h1>SafeLink Local Server is Running!</h1><p>Send POST requests to /api/sos</p>")

    def do_POST(self):
        if self.path == '/api/sos':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            sos_data = json.loads(post_data.decode('utf-8'))
            
            print(f"\n[RECEIVED SOS FROM PHONE] Victim ID: {sos_data.get('victimId')}")
            print(f"Details: {sos_data.get('formData')}")
            
            # Send Success Response
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = {"status": "success", "message": "SOS received at command server!"}
            self.wfile.write(json.dumps(response).encode('utf-8'))

    def do_OPTIONS(self):
        # Handle CORS preflight request
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

# Start Server on 0.0.0.0 (allows connection from both laptop and phone over Wi-Fi)
with socketserver.TCPServer(("0.0.0.0", PORT), SOSHandler) as httpd:
    print(f"Server started! Access it on laptop at: http://localhost:{PORT}")
    print(f"Server listening for incoming phone connections...")
    httpd.serve_forever()