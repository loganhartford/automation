import os
import anthropic
from dotenv import load_dotenv
from db import init_db, already_seen, save_company
import json

load_dotenv()
client = anthropic.Anthropic()

EXTRACTION_PROMPT = """You are reading a startup/tech newsletter. Extract any startups mentioned.

Rules:
- Only include startups and early-stage companies, not large established companies (e.g. Google, Apple)
- Do not include publications, newsletters, or individual people
- Use the company's canonical/official name
- If a company is mentioned multiple times, include it only once
- If no startups are found, return an empty array"""

DEALBREAKER_PROMPT = """You are evaluating a startup to see if it's worth deeper research for a senior embedded/firmware engineer looking to join a hardware startup.

Company: {name}
Description: {description}

Evaluate the company against each of the following criteria and provide a boolean answer and a one-line reason for each."""

REPORT_PROMPT = """You are a startup analyst helping a senior embedded/firmware engineer evaluate companies to join.
Write a thoughtful research report on this company. Be specific where you can, and honest about uncertainty where you can't.
Keep each answer to 2-3 sentences maximum. Be direct and avoid filler phrases.

Company: {name}
Description: {description}

Evaluate the company on each dimension below."""


def extract_companies(newsletter_text: str) -> list:
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[
            {"role": "user", "content": EXTRACTION_PROMPT + "\n\nNewsletter:\n" + newsletter_text}
        ],
        tools=[{
            "name": "save_startups",
            "description": "Save the list of startups extracted from the newsletter",
            "input_schema": {
                "type": "object",
                "properties": {
                    "startups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Official company name"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "One sentence describing what the company does and what problem it solves"
                                }
                            },
                            "required": ["name", "description"]
                        }
                    }
                },
                "required": ["startups"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_startups"}
    )

    return response.content[0].input["startups"]


def check_dealbreakers(name: str, description: str) -> tuple[bool, dict]:
    prompt = DEALBREAKER_PROMPT.format(name=name, description=description)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": "save_dealbreaker_results",
            "description": "Save the dealbreaker evaluation results",
            "input_schema": {
                "type": "object",
                "properties": {
                    "developing_hardware": {
                        "type": "object",
                        "description": "Is the company developing hardware (physical products, chips, sensors, devices, etc.)?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "is_startup": {
                        "type": "object",
                        "description": "Is this a startup (not a large established company)?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "solves_real_problem": {
                        "type": "object",
                        "description": "Does the company solve a real, significant pain point?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "growing_quickly": {
                        "type": "object",
                        "description": "Is the company growing quickly?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "billion_dollar_potential": {
                        "type": "object",
                        "description": "Does this company have the potential to be a billion-dollar company?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    }
                },
                "required": ["developing_hardware", "is_startup", "solves_real_problem", "growing_quickly", "billion_dollar_potential"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_dealbreaker_results"}
    )
    result = response.content[0].input
    passed = all(v["answer"] for v in result.values())
    return passed, result


def generate_report(name: str, description: str, dealbreaker_results: dict) -> dict:
    prompt = REPORT_PROMPT.format(name=name, description=description)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": "save_report",
            "description": "Save the startup research report",
            "input_schema": {
                "type": "object",
                "properties": {
                    "company_size": {
                        "type": "object",
                        "description": "What is the size of the company? 30-100 people is ideal for a senior hire with meaningful equity and impact.",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "monopoly_potential": {
                        "type": "object",
                        "description": "Does this have the potential to be a monopoly in its space?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "novelty": {
                        "type": "object",
                        "description": "Is the company building something wholly new, or combining things in a novel way?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "breakthrough_vs_incremental": {
                        "type": "object",
                        "description": "Is this a breakthrough technology or an incremental improvement on existing solutions?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "timing": {
                        "type": "object",
                        "description": "Is this the right time to be building this company? Why now?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "unique_opportunity": {
                        "type": "object",
                        "description": "Is this company taking advantage of a unique opportunity that others don't see?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    }
                },
                "required": ["company_size", "monopoly_potential", "novelty", "breakthrough_vs_incremental", "timing", "unique_opportunity"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_report"}
    )
    report = response.content[0].input
    report["dealbreakers"] = dealbreaker_results
    return report


def process_newsletter(text: str, source: str = "manual"):
    init_db()
    print(f"\nExtracting companies from newsletter...")
    companies = extract_companies(text)
    print(f"Found {len(companies)} companies: {[c['name'] for c in companies]}")

    for company in companies:
        name = company["name"]
        description = company["description"]

        if already_seen(name):
            print(f"  [{name}] Already in database, skipping.")
            continue

        print(f"  [{name}] Checking dealbreakers...")
        passed, dealbreaker_results = check_dealbreakers(name, description)

        if not passed:
            failed = [k for k, v in dealbreaker_results.items() if not v["answer"]]
            print(f"  [{name}] Failed dealbreakers: {failed}. Skipping.")
            save_company(name, source, passed=False)
            continue

        print(f"  [{name}] Passed! Generating report...")
        report = generate_report(name, description, dealbreaker_results)
        save_company(name, source, passed=True, report=json.dumps(report))
        print(f"  [{name}] Done.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # Usage: python evaluate.py newsletter.txt
        with open(sys.argv[1], "r") as f:
            text = f.read()
        process_newsletter(text, source=sys.argv[1])
    else:
        # Paste mode
        print("Paste newsletter text below. When done, enter a new line with just END:")
        lines = []
        while True:
            line = input()
            if line.strip() == "END":
                break
            lines.append(line)
        process_newsletter("\n".join(lines), source="manual")