# Candidate kit

Employee Attendance & Analytics API built with FastAPI and MongoDB. The application code is in `app/main.py`.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+ (for example, Docker). No Dockerfile is needed. Start MongoDB locally:

```bash
docker run -d --name candidate-kit-mongo -p 27017:27017 mongo:6
```

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db
python -m uvicorn app.main:app --port 8000 --reload
```

The application creates indexes at startup and reads employees and attendance from MongoDB; the database must be populated separately. The provided assignment prompt, sample data and seed script are not part of the public submission. Check readiness at `http://localhost:8000/health`. To stop the database container, run `docker stop candidate-kit-mongo` and then `docker rm candidate-kit-mongo`.

The API follows `openapi.yaml`; the business rules and sample document shapes are in `DATA_MODEL.md`. The assignment's sample dataset is intentionally small and is not included in the public submission.
