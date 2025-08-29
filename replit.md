# Overview

Tree Lead Exchange is a marketplace platform for local tree service companies to buy and sell overflow leads. Sellers can post leads they can't handle, and buyers can purchase leads in their service areas. The platform takes a 1% admin fee only on successful sales and offers refunds within 48 hours if contacts are unreachable.

The system consists of a Flask web application for the frontend interface, n8n workflow automation for backend processing, Airtable as the database, and Stripe for payment processing.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Frontend Architecture
- **Flask Web Application**: Serves HTML templates with Tailwind CSS for styling
- **Template-based UI**: Uses Jinja2 templates for dynamic content rendering
- **Modal-driven Forms**: Forms submit to n8n webhooks via hidden iframes to avoid CORS issues
- **Responsive Design**: Mobile-first design using Tailwind CSS framework

## Backend Architecture
- **n8n Workflow Engine**: Handles all business logic through webhook workflows
- **Webhook-based Processing**: Two main workflows - "New Buyer" and "New Seller" 
- **Airtable Integration**: All data operations routed through Airtable API
- **Email Notifications**: SMTP integration for lead alerts and notifications
- **Session Management**: Flask sessions for admin authentication and user state

## Data Storage
- **Airtable as Primary Database**: Stores leads, businesses, and transaction data
- **Session-based State**: Temporary data stored in Flask sessions
- **File-based Logging**: Application logs stored in local filesystem

## Payment Processing
- **Stripe Integration**: Handles payment processing and checkout flows
- **Lead Purchase Flow**: Direct integration between lead details and Stripe checkout
- **Transaction Tracking**: Payment status tracked in Airtable records

## Lead Matching System
- **Geographic Matching**: Matches leads by ZIP code and service areas
- **Category-based Filtering**: Filters leads by service categories (tree removal, trimming, etc.)
- **Automated Notifications**: n8n workflows automatically alert matching buyers
- **Exclusivity Options**: Supports both exclusive and non-exclusive lead sales

## Authentication & Security
- **Basic Admin Authentication**: Username/password login for admin functions
- **Session-based Security**: Flask sessions manage authentication state
- **API Key Management**: Secure storage of third-party API credentials
- **Environment Variables**: Sensitive configuration stored in .env files

# External Dependencies

## Third-party Services
- **n8n Cloud**: Workflow automation platform hosted at n8n.zach.games
- **Airtable**: Cloud database service for data storage and management
- **Stripe**: Payment processing and checkout functionality
- **GitHub Pages**: Static site hosting for public-facing content

## APIs and Integrations
- **Airtable API**: RESTful API for database operations
- **Stripe API**: Payment processing and transaction management
- **SMTP Services**: Email delivery for notifications and alerts
- **n8n Webhooks**: HTTP endpoints for form submissions and data processing

## Frontend Libraries
- **Tailwind CSS**: Utility-first CSS framework via CDN
- **DataTables**: jQuery plugin for enhanced table functionality
- **jQuery**: JavaScript library for DOM manipulation and AJAX

## Python Dependencies
- **Flask**: Web framework for application routing and templating
- **Requests**: HTTP client for API communications
- **Stripe Python**: Official Stripe SDK for payment processing
- **Python-dotenv**: Environment variable management

## Development Tools
- **Phone Directory Module**: Separate utility for phone number processing and CSV handling
- **SQLite**: Local database for phone directory functionality
- **CSV Processing**: Built-in utilities for data import/export