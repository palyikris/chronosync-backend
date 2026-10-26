from fastapi import APIRouter

from app.api.v1.endpoints import company, invoices, reports

api_router = APIRouter()
api_router.include_router(reports.router, prefix="/reports", tags=["Reports"])
api_router.include_router(company.router, prefix="/company", tags=["Company"])
api_router.include_router(invoices.router, prefix="/invoices", tags=["Invoices"])
