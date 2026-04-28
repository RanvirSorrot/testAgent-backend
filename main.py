from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import test, report, health

app = FastAPI(
    title="TestPilot AI Backend",
    description="Autonomous AI-powered website testing agent",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001", "https://*.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(test.router, prefix="/api", tags=["test"])
app.include_router(report.router, prefix="/api", tags=["report"])


@app.get("/")
def root():
    return {"status": "TestPilot AI is running"}