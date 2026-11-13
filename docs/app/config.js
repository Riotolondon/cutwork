// Where the link helper (cutwork-link/server.py) can be reached. The web app tries each in order.
// Leave the public address empty until a helper is published; a helper on this computer always works.
window.CUTWORK_LINK_SERVERS = [
  'https://desktop-n7gaq4o.tail9d72f3.ts.net',   // public helper
  'http://127.0.0.1:5188',     // helper running on this computer
].filter(Boolean);
