SafeLink: zero-install offline SOS and triage network
Stranded victims connect to a rescue node's Wi-Fi network from any phone, tap I NEED HELP, and their request appears instantly on a rescuer dashboard, ranked by urgency score and triage level. No app installation or cellular/internet connectivity is required. Nodes save every request locally first (store-and-forward) and synchronize upstream to the command node whenever a connection exists.

One Python file (safelink.py), one dependency (Flask).

Bash
pip install flask
Quick Start (Local Demo in 5 Minutes)
Run the network on any laptop and connect devices over local Wi-Fi (mobile hotspot or home router).

1. Command Node (Command Centre / Rescuers)
Run the central server node:

Bash
python safelink.py --role command --node-id CMD --port 5000
Open Victim Page: http://localhost:5000/

Open Rescuer Dashboard: http://localhost:5000/dashboard

2. Relay Node (e.g. Rescue Boat / Patrol Post)
Run a secondary node that receives local victim SOS calls and forwards them to command:

Bash
python safelink.py --role relay --node-id BOAT1 --port 5001 --command-url http://<command-laptop-ip>:5000
Open Relay Victim Page: http://<relay-laptop-ip>:5001/

Open Relay Dashboard: http://<relay-laptop-ip>:5001/dashboard

Key Workflow & Features
Multilingual SOS Form for Victims (/):

One-Tap Instant Dispatch: Tapping I NEED HELP immediately creates a unique UUID-backed case record with location/node metadata.

Comprehensive Triage: Includes multi-language support (English, Hindi, Tamil, Telugu, Malayalam), multi-person/child/infant counters, water depth indicators, mobility status, medical emergencies, immediate danger flags, and landmark/spotting descriptions.

Offline Location Fallback: Uses device GPS if permitted. If GPS is unavailable, it automatically attributes the request location to the node's configured coordinates (--lat / --lng).

Rescuer Dashboard (/dashboard):

Real-time priority ranking into CRITICAL (75+), HIGH (45+), MEDIUM (20+), and LOW levels based on injury, water depth, age group, medical status, and waiting time.

Workflow buttons to update status (Assign team, Rescued, False alarm, or Reopen request).

Integrated offline mapping (Leaflet tile support with interactive SVG fallback locator).

Store-and-Forward Sync:

Every request is stored in SQLite (safelink_<node_id>.db).

Out of Range / Dock Simulation: On a relay node dashboard, click Go out of range (simulate) to pause syncing and simulate going out of range. Requests queue locally on the relay node. Clicking Dock at base & sync flushes the queue back to the command node once reconnected.

Captive Portal Setup (Hardware / Field Deployments)
For standalone field nodes (such as Linux laptops or Raspberry Pis running an AP network):

Bash
sudo python3 safelink.py --role command --port 80 --captive --portal-ip 192.168.4.1 --lat <lat> --lng <lng>
When --captive is enabled, built-in DNS handles domain lookup queries (e.g., Apple/Android connectivity checks) to automatically trigger the "Sign in to network" pop-up on connecting victim smartphones, directing them to the SOS form.

Urgency Scoring Rules
Medical Emergency: +40

Injured Person: +30

Infant / Young Child: +25

Elderly / Disabled: +15

People Count: +2 per person (up to 10 max)

Water Level: Ankle (+3), Knee (+10), Waist (+20), Chest (+32), Roof/Trapped (+42)

Floor Level: Ground floor (+12), 1st floor (+6)

Needs: Medicine (+8), Water (+4), Food (+2)

Waiting Time Bonus: Up to +20 points (+1 point per 5 minutes waiting time for unassigned cases)

API Summary
POST /api/sos — Submit or update an SOS case record.

GET /api/sos — Fetch all active/closed cases, connected nodes, and synchronization status.

GET /api/sos/<sid> — Fetch status and delivery/sync state for a single case.

POST /api/sos/<sid>/status — Update case status (assigned, rescued, false_alarm).

POST /api/sync — Inter-node synchronization endpoint (idempotent record merge).

POST /api/sync-mode — Toggle sync pause (out_of_range vs docked).

GET /api/offline-map-status — Check availability of local Leaflet JS and MBTiles/XYZ offline map data.
