# GHE Cloud (EMU) Inactive Users Report

Find inactive users in a GitHub Enterprise Cloud (EMU) environment by analyzing enterprise audit logs.

## Prerequisites

- Python 3.10+
- GitHub Personal Access Token (PAT) with the following scopes:
  - `admin:enterprise` — to read enterprise members and audit logs
  - `read:org` — to read organization membership

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Using --token flag
python main.py --enterprise <enterprise-slug> --token <ghp_xxx>

# Using GH_TOKEN environment variable
export GH_TOKEN=ghp_xxx
python main.py --enterprise <enterprise-slug>

# Custom inactivity threshold (60 days) and output file
python main.py --enterprise <enterprise-slug> --days 60 --output my_report.csv

# Verbose mode for debugging
python main.py --enterprise <enterprise-slug> -v
```

## Options

| Flag             | Description                                      | Default                           |
| ---------------- | ------------------------------------------------ | --------------------------------- |
| `--enterprise`   | GitHub Enterprise slug (required)                | —                                 |
| `--token`        | GitHub PAT                                       | `$GH_TOKEN` env var               |
| `--days`         | Inactivity threshold in days                     | `90`                              |
| `--output`       | Output CSV file path                             | `inactive_users_YYYY-MM-DD.csv`   |
| `--verbose, -v`  | Enable verbose/debug logging                     | Off                               |

## Output

The tool generates a CSV file with the following columns:

| Column               | Description                                    |
| -------------------- | ---------------------------------------------- |
| `login`              | GitHub username                                |
| `last_activity_date` | Last activity timestamp (or "Never")           |
| `days_inactive`      | Number of days since last activity             |
| `status`             | `inactive` or `inactive (no activity found)`   |

## How It Works

1. **Fetches all enterprise members** via `GET /enterprises/{slug}/members`
2. **Queries audit logs** via `GET /enterprises/{slug}/audit-log` for the specified time window
3. **Compares** each member against audit log activity — members with no entries (or entries older than the threshold) are marked inactive
4. **Writes** the results to a CSV file
