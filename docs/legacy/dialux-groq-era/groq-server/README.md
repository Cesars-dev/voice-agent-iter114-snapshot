# Retell-Groq Middleware

FastAPI WebSocket server that bridges Retell AI and Groq for low-latency LLM responses.

## Architecture
```
Retell ──WebSocket──► this server ──HTTP──► Groq API
                      /llm-websocket         llama-3.1-70b-versatile
```

## Deploy to VPS

### 1. Copy files to VPS
```bash
scp -r ./groq-server vps-julio:/home/julio/projects/VoiceAI/
```

### 2. SSH in and install dependencies
```bash
ssh vps-julio
cd /home/julio/projects/VoiceAI/groq-server
pip install -r requirements.txt
```

### 3. Set up environment
```bash
cp .env.template .env
nano .env   # fill in GROQ_API_KEY and RETELL_API_KEY
```

### 4. Install and start systemd service
```bash
sudo cp retell-groq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable retell-groq
sudo systemctl start retell-groq
```

### 5. Verify it's running
```bash
sudo systemctl status retell-groq
# Should show: active (running)

# Test WebSocket endpoint is reachable
curl http://localhost:8080/
```

## Point Retell Agent to This Server

In Retell dashboard, update the clone agent's LLM:
- Response Engine Type: `custom_llm`
- WebSocket URL: `wss://YOUR_VPS_IP:8080/llm-websocket`

Or via API:
```bash
curl -X PATCH https://api.retellai.com/update-agent/YOUR_AGENT_ID \
  -H "Authorization: Bearer $RETELL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"response_engine": {"type": "custom_llm", "llm_websocket_url": "wss://YOUR_VPS_IP:8080/llm-websocket"}}'
```

## View Logs
```bash
sudo journalctl -u retell-groq -f
```

## Notes
- Port 8080 must be open in VPS firewall: `sudo ufw allow 8080`
- If VPS is behind a load balancer with SSL, use `wss://` in Retell; otherwise use `ws://`
- Tool calls (extract_discovery_details, etc.) are NOT supported in this v1 — variable extraction happens in the prompt only
