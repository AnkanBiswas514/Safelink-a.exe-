                                                      🛰️ SafeLink 
Zero-Install Offline Emergency SOS & Priority Triage Network

SafeLink is a disaster-resilient, offline emergency response network that enables stranded victims to request rescue using any smartphone without cellular network coverage, internet access, or pre-installed mobile apps.

Victims simply join an open Wi-Fi network hosted by a rescue node (such as a rescue boat, drone, patrol vehicle, or ground base) and tap I NEED HELP. Requests are saved locally, scored by an automated triage algorithm, and synchronized back to the Command Centre dashboard using a store-and-forward mesh mechanism.

🌟 Key Features
📱 Zero-Install Victim App: Served directly over local HTTP / captive portal. Works on iOS, Android, and web browsers with zero setup.

🌐 Fully Offline Operation: Zero reliance on cloud servers, cellular towers, or internet connectivity.

🚢 Store-and-Forward Mesh Network: Rescue boats and mobile units store emergency requests in local SQLite databases (safelink_<node_id>.db) and automatically sync them to the Command Centre when in radio range.

🚨 Algorithmic Priority Triage: Transparent, rule-based urgency scoring ranks incoming cases into CRITICAL, HIGH, MEDIUM, and LOW tiers based on medical state, flood depth, vulnerable individuals, and waiting duration.

🗺️ Interactive Rescuer Dashboard: Command center dashboard featuring real-time status management (Assign team, Rescued, False alarm), offline Leaflet map integration, and case timelines.

🌍 Multilingual Support: Supports English, Hindi, Tamil, Telugu, and Malayalam.

🏗️ Architecture Overview
[ Victim Phone ] ──Wi-Fi / Captive Portal──> [ Relay Node / Boat (BOAT1) ]
                                                      │
                                           (Store & Forward Sync)
                                                      │
                                                      ▼
[ Rescuer Dashboard ] <─── HTTP / REST ───> [ Command Node (CMD) ]
🚀 Quick Start (Local Demo)
Prerequisites
Python 3.8+

Flask (pip install flask)

Step 1: Start the Command Centre Node
Run the central command node on your primary laptop:

Bash
python safelink.py --role command --node-id CMD --port 5000
Rescuer Dashboard: http://localhost:5000/dashboard

Victim SOS Form: http://localhost:5000/

Step 2: Start a Relay Node (e.g., Rescue Boat)
Run a secondary node representing a mobile rescue unit:

Bash
python safelink.py --role relay --node-id BOAT1 --port 5001 --command-url http://<command-laptop-ip>:5000
Relay SOS Form: http://<relay-laptop-ip>:5001/

Relay Dashboard: http://<relay-laptop-ip>:5001/dashboard

TECH STACK: Python, SQLite

📊 Priority Triage Engine
The urgency engine evaluates each request and assigns points based on risk metrics:

Category	Indicator	Points
Emergency Type	Medical Emergency	+40
Injured Person	+30
Vulnerable Individuals	Infant / Young Child (< 5 yrs)	+25
Elderly / Disabled	+15
People Count	+2 per person (max +20)
Water Level	Ankle / Knee / Waist / Chest / Roof	+3 to +42
Location Risk	Ground Floor	+12
First Floor	+6
Essential Needs	Medicine / Drinking Water / Food	+2 to +8
Wait Bonus	Duration awaiting rescue	+1 / 5 mins (max +20)
Triage Levels:
🔴 CRITICAL: Score 
≥75

🟠 HIGH: Score 
45−74

🟡 MEDIUM: Score 
20−44

🟢 LOW: Score 
<20

📡 Captive Portal Deployment (Field Mode)
For hardware deployments on Linux devices or Raspberry Pis acting as access points:

Bash
sudo python3 safelink.py --role command --port 80 --captive --portal-ip 192.168.4.1 --lat 12.9716 --lng 77.5946
When victims join the open network, the built-in DNS intercepts connectivity checks and launches the I NEED HELP interface automatically via operating system captive portal popups.

🛠️ REST API Specification
Endpoint	Method	Description
/api/sos	POST	Submit or update an emergency SOS request
/api/sos	GET	Retrieve all active and closed cases
/api/sos/<sid>	GET	Get delivery status of a specific request
/api/sos/<sid>/status	POST	Update case status (assigned, rescued, false_alarm)
/api/sync	POST	Bidirectional node-to-node database sync
/api/sync-mode	POST	Toggle offline queueing mode (out_of_range / docked)
/api/offline-map-status	GET	Query local Leaflet asset and MBTiles status
⚙️ Repository Structure
.
├── safelink.py                 # Core single-file node backend & frontend
├── SafeLink_rescue_simulation_5_boats.html  # Interactive 5-boat Canvas simulation
├── static/                     # Offline Leaflet JS assets and MBTiles
└── README.md                   # Project documentation
🤝 Contributing
Contributions are welcome! Feel free to open an issue or submit a pull request for new features, localized translations, or enhanced UI components.
