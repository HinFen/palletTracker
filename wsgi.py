"""Entry point for hosts that run Flask apps through WSGI (for example PythonAnywhere or gunicorn).
The WSGI object is called `application` (and `app`)."""
from tracker import create_app

application = create_app()
app = application
