# Main entrypoint file for Gunicorn / WSGI server on Azure App Service.
# Delegates all routing and application logic to the new modular app.py.

from main import application

if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    import os
    
    HOST = "0.0.0.0"
    PORT = int(os.environ.get("PORT", "8000"))
    
    print(f"Starting local WSGI server on http://localhost:{PORT}...", flush=True)
    try:
        httpd = make_server(HOST, PORT, application)
        print(f"Server running. Open http://127.0.0.1:{PORT}/ in your browser.", flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local server...", flush=True)
