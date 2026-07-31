# ChronoSync Backend

## Overview

ChronoSync Backend is a FastAPI-based service designed to support the operational and reporting needs of the ChronoSync platform. Its primary responsibility is to provide secure, authenticated access to company timesheet data and transform that data into professionally formatted Excel attachments for invoicing and client reporting purposes.

The application is intentionally focused on a single, high-value business workflow: generating invoice attachment documents, commonly referred to as "számlamelléklet" in Hungarian, from aggregated timesheet data retrieved from Supabase. It combines modern API development practices, authentication-aware data access, and document generation into a compact and maintainable backend service.

## Purpose

At its core, this application enables a user to:

- authenticate with a bearer token obtained from Supabase Auth,
- retrieve timesheet and project data for a specific company,
- aggregate work hours by client and project,
- generate an Excel workbook containing one sheet per client,
- stream the resulting file back to the client application for download.

This makes the backend a bridge between operational data in Supabase and business documentation used in financial and client-facing workflows.

## Key Features

- FastAPI-based REST API with automatic OpenAPI documentation
- Secure access through Bearer token validation
- Supabase integration with Row Level Security (RLS) awareness
- Aggregation of billed or logged hours by client and project
- Excel report generation with client-specific sheets
- Language-aware document text for Hungarian and English output
- Streaming of generated Excel files for direct download
- Health endpoint for service availability checks

## Architecture

The project is structured around a small set of clearly separated responsibilities:

- API layer: exposes the endpoints consumed by the frontend or other services
- Service layer: handles business logic for report generation and data retrieval
- Schema layer: defines request models and payload validation
- Configuration layer: loads environment variables for Supabase access

### Main components

- [main.py](main.py): application entry point that imports the FastAPI app
- [app/main.py](app/main.py): application initialization, CORS configuration, and router registration
- [app/api/v1/router.py](app/api/v1/router.py): registers API version 1 routes
- [app/api/v1/endpoints/reports.py](app/api/v1/endpoints/reports.py): report generation endpoint and request handling
- [app/services/supabase_service.py](app/services/supabase_service.py): communication with Supabase and data aggregation
- [app/services/excel_service.py](app/services/excel_service.py): Excel workbook generation and formatting
- [app/schemas/reports.py](app/schemas/reports.py): request model for report generation
- [app/core/config.py](app/core/config.py): environment-based configuration loader

## Project Structure

```text
chronosync-backend/
├── app/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       │   └── reports.py
│   │       └── router.py
│   ├── core/
│   │   └── config.py
│   ├── schemas/
│   │   └── reports.py
│   ├── services/
│   │   ├── excel_service.py
│   │   └── supabase_service.py
│   └── main.py
├── assets/
├── main.py
├── requirements.txt
└── README.md
```

## Technology Stack

The application is built using the following primary technologies:

- Python 3.13+
- FastAPI for API development
- Uvicorn as the ASGI server
- Pydantic for request validation and models
- Supabase Python client for database access
- OpenPyXL for Excel workbook generation
- python-dotenv for environment management

## Prerequisites

Before running the application locally, ensure the following are available:

- Python 3.13 or newer
- pip or another package installer
- Access to a Supabase project
- Valid Supabase URL and anonymous key values
- A frontend or API client capable of sending authenticated requests

## Environment Configuration

The backend expects the following environment variables to be present in a `.env` file at the project root:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
```

These values are loaded by the configuration module and used to create the Supabase client used for data access.

## Installation

It is recommended to use a virtual environment for local development.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running the Application

Start the development server with:

```bash
uvicorn main:app --reload --host localhost --port 8000
```

Once running, the API will be available at:

- http://localhost:8000/docs for Swagger UI
- http://localhost:8000/redoc for ReDoc
- http://localhost:8000/health for a simple health check

## API Endpoints

### Health check

- GET /health

Returns a simple JSON payload confirming that the service is running.

### Generate invoice attachment report

- POST /api/v1/reports/generate-szamlamelleklet

This endpoint accepts a request body with client code selection, date range filters, and a period label. The backend then:

1. extracts the Bearer token from the Authorization header,
2. authenticates with Supabase using the supplied token,
3. queries timesheet data and keeps only entries for the selected client codes,
4. aggregates hours by client and project,
5. creates an Excel workbook with one sheet per client,
6. returns the file as a downloadable attachment.

## Request Payload

The request body expects the following structure:

```json
{
  "client_codes": ["COS", "ABC"],
  "start_date": "2026-01-01",
  "end_date": "2026-01-31",
  "period_text": "2026 január"
}
```

### Request headers

```http
Authorization: Bearer <supabase-jwt>
```

## Example Usage

### cURL example

```bash
curl -X POST "http://localhost:8000/api/v1/reports/generate-szamlamelleklet" \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "client_codes": ["COS", "ABC"],
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "period_text": "2026 január"
  }' \
  -o report.xlsx
```

## Report Generation Details

The generated workbook is designed to be suitable for client-facing invoice attachments. Each client receives a dedicated worksheet, and the report includes:

- client name,
- document title,
- reporting period,
- a short descriptive statement,
- a task and hours table,
- a footer logo when available.

The document content is localized through a small language map and currently supports Hungarian and English text variants.

## Notes on Data Access

The backend relies on Supabase data models and uses the authenticated user’s JWT for access. This design enables the application to respect Supabase Row Level Security policies and ensures that data access is scoped to the user’s permissions rather than bypassed through a service role.

## Development Notes

The current implementation focuses on a single reporting workflow and is intentionally compact. It is well-suited for extension if additional reporting types, export formats, or richer business rules are introduced later.

## Summary

ChronoSync Backend is a focused, production-oriented service for turning structured timesheet data into polished and client-ready Excel reports. Its design emphasizes clarity, maintainability, security, and operational usefulness, making it a strong foundation for further growth within the ChronoSync ecosystem.
