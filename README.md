# Browser AI VNC - Shipping Line Tracking Automation

A comprehensive Docker-based solution for AI-driven shipping line tracking automation with built-in evaluation capabilities.

## 🎯 Overview

This project provides two main components:

1. **Automation Container**: A browser automation environment with VNC access and REST API
2. **Evaluation Container**: Automated testing and evaluation of the automation container

## 🚀 Quick Start

### Run Full Evaluation Suite

```bash
# Run both automation and evaluation containers
docker compose up --build

# The evaluation container will automatically test the automation container

# Access points during testing:
# - VNC Viewer: localhost:5900 (password: secret)
# - Web VNC: http://localhost:6080
# - API: http://localhost:5001 (configurable via API_PORT env var)
```

## 📁 Project Structure

```
browseraivnc/
├── src/                        # Automation scripts
│   ├── api.py                  # Flask API for automation
│   └── main-simple-test.py     # Visual automation demo
├── evaluator/                  # Evaluation container
│   ├── src/
│   │   └── evaluator.py        # Simple evaluation script
│   ├── Dockerfile             # Evaluation container image
│   └── requirements.txt       # Evaluation dependencies
├── docker-compose.yml          # Full evaluation setup
└── README.md                   # This file
```

## 🤖 Automation Container

### Features

- **VNC Access**: Visual browser automation with remote desktop
- **REST API**: Programmatic access to automation functions
- **Caching**: Intelligent in-memory caching for repeated requests
- **Multi-Browser**: Support for Chrome, Firefox, and Chromium

### API Endpoints

#### Health Check
```bash
GET /health
```

#### Track Shipping
```bash
POST /track
Content-Type: application/json

{
  "booking_id": "SINI25432400",
  "force_fresh": false
}
```



### Example API Usage

```python
import requests

# Health check
response = requests.get("http://localhost:5001/health")
print(response.json())

# Track a booking
payload = {"booking_id": "SINI25432400"}
response = requests.post("http://localhost:5001/track", json=payload)
print(response.json())
```

## 🧪 Evaluation Container

### Purpose

The evaluation container automatically tests the automation container to ensure:

- ✅ Tracking returns correct voyage number and arrival date
- ✅ Caching improves performance on repeat requests
- ✅ System works reliably

### Evaluation Tests

1. **Fresh Tracking Test**: Tests initial tracking request and verifies correct data (YM MANDATE 0096W, 2025-02-28)
2. **Cached Tracking Test**: Tests repeat request is faster and uses cache

### Running Evaluation

```bash
# Run full evaluation suite
docker compose up --build

# View evaluation logs
docker logs evaluation_container
```

### Evaluation Report Example

```
🧪 Starting Simple Evaluation
========================================
⏳ Waiting for API to be ready...
✅ API is ready

🚢 Testing Fresh Request...
✅ Fresh request passed (12.3s)

💾 Testing Cached Request...
✅ Cached request passed (2.1s)

📊 Results: 2/2 tests passed
🎉 All tests passed!
```

## 🔧 Configuration

### Environment Variables

#### Automation Container
- `DISPLAY`: X11 display (default: :99)
- `SCREEN_WIDTH`: Screen width (default: 1920)
- `SCREEN_HEIGHT`: Screen height (default: 1080)
- `BROWSER`: Browser choice (chrome/firefox/chromium)

#### Evaluation Container
- `AUTOMATION_API_URL`: API URL (default: http://automation:5000)
- `PYTHONUNBUFFERED`: Python output buffering (default: 1)

### Volume Mounts

None - all evaluation output is shown in console logs

## 🐳 Docker Commands

### Build and Run

```bash
# Build automation container
docker compose build

# Run automation container
docker compose up -d

# Run evaluation suite
docker compose up --build

# Stop all containers
docker compose down
```

### Debugging

```bash
# View automation container logs
docker logs automation_container

# View evaluation container logs
docker logs evaluation_container

# Execute commands in running container
docker exec -it automation_container bash
```

## 📋 Assignment Context

This project was developed for the **AI-based Shipping Line Tracking Assignment** with the following requirements:

### Assignment Goals
1. **Natural Language Processing**: Use AI to interpret tracking requests
2. **Process Persistence**: Cache and reuse automation steps
3. **Adaptability**: Handle different booking IDs without hardcoding
4. **Repeatability**: Consistent results across multiple runs

### Sample Booking ID
- `SINI25432400` (Test case with voyage: YM MANDATE 0096W, arrival: 2025-02-28)

### Expected Outputs
- **Voyage Number**: "YM MANDATE 0096W"
- **Arrival Date**: "2025-02-28"
- **Execution Time**: Performance metrics
- **Cache Status**: Whether cached data was used

## 🔍 Troubleshooting

### Common Issues

1. **Container won't start**
   ```bash
   # Check port conflicts
   docker ps -a
   
   # Clean up containers
   docker system prune -f
   ```

2. **VNC connection fails**
   ```bash
   # Check VNC logs
   docker logs automation_container | grep vnc
   
   # Restart container
   docker-compose restart
   ```

3. **API not responding**
   ```bash
   # Check API health
   curl http://localhost:5001/health
   
   # Check container logs
   docker logs automation_container | grep Flask
   ```

4. **Evaluation fails**
   ```bash
   # Check evaluation logs
   docker logs evaluation_container
   
   # Verify network connectivity
   docker network ls
   ```


## 📊 Metrics and Monitoring


### Monitoring Commands
```bash
# Monitor container resources
docker stats

# Check disk usage
docker system df

# Monitor API requests
docker logs automation_container | grep -E "(GET|POST)"
```
