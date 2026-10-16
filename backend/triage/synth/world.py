"""Static vocabulary and tenant definitions for the synthetic world."""
from __future__ import annotations

TENANTS = [
    # The tenants are three B2B SaaS companies using Triage. Uneven sizes on
    # purpose: Quill is small enough that a per-tenant model would starve.
    dict(tenant_id="northwind", name="Northwind Analytics", timezone="America/New_York",
         n_accounts=3000, n_csms=20, churn_bias=0.0, seat_price=420, capacity=15),
    dict(tenant_id="helios", name="Helios Workforce", timezone="Europe/London",
         n_accounts=1500, n_csms=10, churn_bias=-0.3, seat_price=360, capacity=15),
    dict(tenant_id="quill", name="Quill Commerce", timezone="Asia/Kolkata",
         n_accounts=500, n_csms=4, churn_bias=0.45, seat_price=240, capacity=12),
]

NAME_HEADS = """Acme Apex Arbor Aspen Atlas Aurora Beacon Birch Blue Bright Brightline Cascade Cedar
Cinder Clear Cobalt Copper Crest Crimson Delta Ember Evergreen Falcon Fern Flint Forge Fox Granite
Harbor Hawk Helix Horizon Indigo Iron Ivy Jade Juniper Kestrel Keystone Lark Ledger Linden Lumen
Maple Meridian Mesa Monarch Nimbus North Nova Oak Onyx Orchid Orbit Osprey Peak Pine Pioneer Prism
Quarry Quartz Raven Redwood Ridge River Sable Sage Sequoia Sierra Silver Slate Solstice Sparrow
Spruce Stone Summit Tandem Terra Thistle Tidal Timber Trail True Union Vale Vector Verde Vista Willow
Wren Zenith""".split()

NAME_TAILS = [
    ("Logistics", "Logistics"), ("Health", "Healthcare"), ("Dental Group", "Healthcare"),
    ("Freight", "Logistics"), ("Capital", "Financial Services"), ("Partners", "Professional Services"),
    ("Labs", "Software"), ("Systems", "Software"), ("Software", "Software"), ("Analytics", "Software"),
    ("Retail", "Retail"), ("Foods", "Consumer Goods"), ("Brands", "Consumer Goods"),
    ("Manufacturing", "Manufacturing"), ("Industries", "Manufacturing"), ("Energy", "Energy"),
    ("Solar", "Energy"), ("Insurance", "Financial Services"), ("Bank", "Financial Services"),
    ("Media", "Media"), ("Studios", "Media"), ("Education", "Education"), ("Academy", "Education"),
    ("Hospitality", "Hospitality"), ("Hotels", "Hospitality"), ("Realty", "Real Estate"),
    ("Properties", "Real Estate"), ("Clinics", "Healthcare"), ("Pharma", "Healthcare"),
    ("Robotics", "Manufacturing"), ("Telecom", "Telecom"), ("Networks", "Telecom"),
    ("Consulting", "Professional Services"), ("Legal", "Professional Services"),
    ("Marine", "Logistics"), ("Air", "Logistics"), ("Motors", "Automotive"), ("Mobility", "Automotive"),
    ("Biotech", "Healthcare"), ("Payments", "Financial Services"),
]

LEGAL_SUFFIXES = ["", "", "", " Inc.", " LLC", " Ltd", " Group", " Co."]

FIRST_NAMES = """Aarav Aisha Alex Amelia Ana Arjun Ben Carla Chen Chloe Daniel Diego Elena Emma Ethan
Farah Felix Grace Hannah Hiro Isaac Ishaan Jade James Jonas Julia Kavya Kenji Leah Liam Lucas Maya
Mei Mia Nadia Naomi Noah Olivia Omar Priya Rahul Riya Rosa Sam Sara Sofia Tariq Theo Uma Victor
Wei Yara Zoe""".split()

LAST_NAMES = """Adams Ahmed Bauer Brooks Chen Costa Das Diaz Evans Fischer Garcia Gupta Hall Ito Iyer
Jensen Khan Kim Kumar Lee Lopez Martin Mehta Meyer Moreau Murphy Nair Nguyen Novak Okafor Patel Perez
Rao Reed Rossi Sato Schmidt Shah Silva Singh Smith Tanaka Taylor Varga Wang Weber Wilson Young Zhang""".split()

CONTACT_TITLES = ["VP Operations", "Head of Analytics", "Director of IT", "Operations Manager",
                  "CFO", "Program Manager", "Team Lead", "Data Engineer", "COO"]

CONTACT_TZS = ["America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London",
               "Europe/Berlin", "Asia/Kolkata", "Australia/Sydney", "Asia/Singapore"]

# Email subject/snippet pools keyed by tone. Snippets are what an LLM brief may
# quote, so they carry the human texture a feature vector cannot.
EMAIL_INBOUND = {
    "positive": [
        ("Re: Quarterly review", "Thanks for the session — the team is getting a lot out of the new dashboards."),
        ("Adding more users", "We'd like to roll this out to two more teams next month. Can you send pricing?"),
        ("Re: Training", "The training was great, adoption has picked up across the ops team."),
        ("Quick win", "Just wanted to say the automation saved us hours this week."),
    ],
    "neutral": [
        ("Re: Check-in", "Sounds good, let's catch up after our planning cycle."),
        ("Question on SSO", "Can you point us to the docs for configuring SSO?"),
        ("Re: Invoice", "Received, forwarding to finance."),
        ("Re: Release notes", "Thanks, will share with the team."),
    ],
    "negative": [
        ("Re: Ongoing issues", "Honestly the reliability problems are making it hard to justify this internally."),
        ("Escalation", "This is the third time this month the sync has failed. We need a fix and a timeline."),
        ("Re: Check-in", "We're reviewing all our tools this quarter, will get back to you."),
        ("Usage concerns", "Adoption has been low on our side, not sure the team sees the value yet."),
    ],
    "competitor": [
        ("Data export", "Can you tell us how to export all our historical data and reports?"),
        ("Contract question", "What is the notice period on our current agreement?"),
    ],
    "budget": [
        ("Seat count for next term", "We need to reduce seats next term, budgets were cut across the board."),
        ("Re: Invoice", "Finance has put a hold on new spend, payment may be delayed."),
    ],
}

EMAIL_OUTBOUND = [
    ("Checking in", "Hi — wanted to check in on how things are going and whether there's anything we can help with."),
    ("Quarterly review", "Would love to set up a quarterly review to go over usage and goals."),
    ("Renewal planning", "Your renewal is coming up — can we find 30 minutes to plan for next term?"),
    ("Following up on your ticket", "Following up on the recent support issue — is everything resolved on your side?"),
    ("New features", "Sharing a few new features that map to the goals you mentioned."),
]

TICKET_SUBJECTS = {
    "bug": ["Report export fails with timeout", "Dashboard not loading for some users",
            "Data sync stuck since yesterday", "Error 500 when saving filters", "Charts show wrong totals"],
    "how_to": ["How do I schedule a report?", "Setting up user roles", "Configure SSO",
               "Connect to our warehouse", "Bulk invite users"],
    "billing": ["Invoice address change", "Question about our invoice", "Update payment method"],
    "feature_request": ["Request: more chart types", "Request: API rate limit increase", "Request: audit log export"],
    "outage": ["URGENT: platform down for our team", "Sync failing for all workspaces", "Login failures across org"],
    "data_export": ["Bulk export of all historical data", "Export all reports to CSV"],
}
