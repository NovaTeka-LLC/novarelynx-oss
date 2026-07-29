NovaRelynx -- Setup Instructions (Linux)
=========================================

1. Open a terminal in this folder and run:
     chmod +x start.sh
     ./start.sh

   The first time, it checks for Python 3 and installs two small packages
   (httpx, websockets) into your user site-packages. This only happens once.
   A browser tab opens automatically at http://127.0.0.1:8787 (if you're on
   a desktop -- on a headless machine, just open that address yourself from
   another device on the same network, or via an SSH tunnel).

2. If Python 3 isn't installed, start.sh will tell you the right command for
   your distro, then stop. Install it and run ./start.sh again.

3. In the browser tab:
   - Paste the API key you were given, and click Save.
   - Click "Add an app", give it a name, and enter the local port (or full
     URL) of the thing you want to make reachable from the internet
     (e.g. "8000" for something running on http://127.0.0.1:8000).
   - Click "Add + start tunnel". Your app is now live at the address shown
     next to it -- share that link with anyone, it works from anywhere.
   - Use Stop / Start / Remove any time.

4. Keep the terminal running start.sh open in the background while you want
   your tunnels running. Closing it stops them. Run ./start.sh again anytime
   to bring them back -- it remembers your apps.

That's it. No port forwarding, no router configuration, nothing to open on
your firewall -- this machine only ever makes OUTBOUND connections.

Questions? support@novarelynx.com
