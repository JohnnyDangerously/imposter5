# IMPOSTER5 🛡️

**Imposter5** is a standalone, state-of-the-art Red Team Human Mechanics Simulation & Evasion Suite. It models, executes, and evaluates human-like browser automation trajectories (mouse movements, scrolls, hovers, clicks) against advanced evasion detection protocols (like `fp-agent` and `mus.js`).

Featuring a beautiful, highly colorful, and futuristic cyberpunk UI, Imposter5 allows you to:
- **Model Target Websites**: Run simulations against any URL (with specialized support for Wikipedia and LinkedIn Feed).
- **Assign Behavior Packs**: Choose from multiple human personas (e.g., *Curious Reader*, *Focused Power User*, *Impatient Scanner*) and completion depths.
- **Toggle Engagement Techniques**: Enable bidirectional scrolls, comments expansion, profile peeks, notification checks, and hovers.
- **Tune Mouse Physics**: Customize trajectory wobble, steps, overshoot, burst sizes, and click delays.
- **Visual Watch & Capture**: Watch the live headed browser pop up, and play back recorded videos with a bright red **HUMAN MOUSE** cursor overlay.
- **Evasion Scoring**: Instantly evaluate trajectories against the `fp-agent` XGBoost classifier to see if they evade detection.

---

## Architecture

- **Backend**: FastAPI server that interfaces with Playwright and CloakBrowser to launch stealth browser sessions, inject synthetic mouse overlays, and run the `fp-agent` featurizer and classifier.
- **Frontend**: Standalone React + Vite + Tailwind CSS SPA featuring a cyberpunk dashboard, live logging console, and custom video player.

---

## Getting Started

### Prerequisites

- Python 3.11 or higher (with `uv` or `pip`)
- Node.js 18 or higher (with `npm` or `yarn`)
- Playwright browser binaries

### Installation

1. **Clone the repository**:
   ```bash
   git clone git@github.com:JohnnyDangerously/imposter5.git
   cd imposter5
   ```

2. **Set up the Backend**:
   ```bash
   # Create a virtual environment and install dependencies
   cd backend
   uv venv
   source .venv/bin/activate
   uv pip install -r ../requirements.txt
   playwright install chromium
   ```

3. **Set up the Frontend**:
   ```bash
   cd ../frontend
   npm install
   ```

---

## Running the Application

### 1. Start the Backend Server
From the `backend` directory (with virtual environment active):
```bash
python app.py
```
The backend will start on `http://127.0.0.1:5180`.

### 2. Start the Frontend Dev Server
From the `frontend` directory:
```bash
npm run dev
```
The frontend will start on `http://localhost:5173` and automatically proxy API requests to the backend.

### 3. Build for Production
To build the frontend and serve it directly from the FastAPI backend:
```bash
cd frontend
npm run build
```
Then start the backend with `python app.py` and navigate to `http://127.0.0.1:5180`.

---

## License

MIT License. Developed by Johnny Dangerously.
