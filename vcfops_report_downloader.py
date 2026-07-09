#!/usr/bin/env python3
import os
import sys
import time
import argparse
import configparser
from datetime import datetime, timedelta, timezone
import requests
import urllib3


def parse_arguments_and_config():
    """Parses CLI arguments and merges them with an optional config file."""
    parser = argparse.ArgumentParser(
        description="Download VCF Operations reports generated in the last 24 hours."
    )

    parser.add_argument(
        "-c", "--config", help="Retrieve configuration from an INI file"
    )
    parser.add_argument("-H", "--host", help="VCF Operations FQDN or IP")
    parser.add_argument("-u", "--user", help="VCF Operation user")
    parser.add_argument("-p", "--password", help="VCF Operations password")
    parser.add_argument(
        "-a",
        "--authsource",
        help="VCF Operations Authentication source (default is internal)",
    )
    parser.add_argument("-o", "--outdir", help="Directory in which to save PDFs")
    parser.add_argument(
        "-U",
        "--unsafe",
        action="store_true",
        help="Skip SSL verification (not recommended in production)",
    )

    args = parser.parse_args()
    config_values = {}

    # 1. Process config file if provided
    if args.config:
        if not os.path.exists(args.config):
            parser.error(f"Configuration file not found: {args.config}")

        # Security Check: Verify strict file permissions (chmod 600) on POSIX systems
        if os.name == "posix":
            file_mode = os.stat(args.config).st_mode
            if file_mode & 0o077:
                print(
                    f"[WARNING] Security Risk: Configuration file {args.config} has insecure permissions!"
                )
                print("          It is highly recommended to run: chmod 600 <file>")

        config = configparser.ConfigParser()
        try:
            config.read(args.config)
            if "vcf-ops" in config:
                config_values = dict(config["vcf-ops"])
            else:
                parser.error(
                    f"Missing required [vcf-ops] section in config file {args.config}"
                )
        except Exception as e:
            parser.error(f"Failed to parse config file: {e}")

    # 2. Merge values: CLI flags take absolute priority over config file defaults
    final_params = {
        "host": args.host or config_values.get("host"),
        "user": args.user or config_values.get("user"),
        "password": args.password or config_values.get("password"),
        "authsource": args.authsource
        or config_values.get("authsource")
        or "internal",
        "outdir": args.outdir or config_values.get("outdir"),
        "unsafe": args.unsafe,
    }

    # 3. Validation: Ensure all essential parameters are populated
    missing_fields = []
    for field in ["host", "user", "password", "outdir"]:
        if not final_params[field]:
            missing_fields.append(f"--{field}")

    if missing_fields:
        parser.error(
            f"Missing required parameters: {', '.join(missing_fields)}. "
            f"You must specify them via CLI flags or within a config file (-c)."
        )

    return final_params


def get_auth_token(host, username, password, auth_source, verify_ssl):
    """Authenticates to VCF Operations and retrieves an API token."""
    url = f"https://{host}/suite-api/api/auth/token/acquire"
    
    payload = {
        "username": username,
        "password": password
    }
    
    if auth_source and auth_source.lower() != "internal":
        payload["authSource"] = auth_source

    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    print(f"[*] Authenticating to VCF Operations: {host}")
    response = requests.post(
        url, json=payload, headers=headers, verify=verify_ssl
    )
    response.raise_for_status()

    return response.json().get("token")


def get_report_definitions(host, token, verify_ssl):
    """Fetches all report templates to map UUIDs to human-friendly names."""
    url = f"https://{host}/suite-api/api/reportdefinitions"
    headers = {
        "Authorization": f"vRealizeOpsToken {token}",
        "Accept": "application/json",
    }
    params = {"pageSize": 1000}

    try:
        print("[*] Pre-fetching report templates to resolve human-friendly names...")
        response = requests.get(
            url, headers=headers, params=params, verify=verify_ssl
        )
        response.raise_for_status()

        definitions = response.json().get("reportDefinitions", [])

        template_map = {
            defn.get("id"): defn.get("name")
            for defn in definitions
            if defn.get("id") and defn.get("name")
        }
        print(f"[+] Successfully mapped {len(template_map)} report templates.")
        return template_map

    except Exception as e:
        print(
            f"[!] Warning: Could not resolve template names ({e}). Defaulting to 'Untitled_Report'."
        )
        return {}


def download_recent_reports(host, token, outdir, verify_ssl):
    """Polls report instances and downloads those finished in the last 24 hours."""
    template_map = get_report_definitions(host, token, verify_ssl)

    # Clean, modern timezone-aware UTC datetime tracking
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

    url = f"https://{host}/suite-api/api/reports"
    headers = {
        "Authorization": f"vRealizeOpsToken {token}",
        "Accept": "application/json",
    }
    params = {"status": "COMPLETED", "pageSize": 1000}

    print("[*] Fetching completed report instances...")
    response = requests.get(
        url, headers=headers, params=params, verify=verify_ssl
    )
    response.raise_for_status()

    all_reports = response.json().get("reports", [])
    download_count = 0

    print(
        f"[*] Found {len(all_reports)} total completed reports. Filtering for the last 24 hours..."
    )

    for report in all_reports:
        report_id = report.get("id")
        completion_time_str = report.get("completionTime")

        if not completion_time_str:
            continue

        template_id = report.get("reportDefinitionId") or report.get(
            "reportTemplateId"
        )
        report_name = template_map.get(template_id, "Untitled_Report")

        try:
            parts = completion_time_str.split()
            if len(parts) == 6:
                clean_date_str = f"{parts[0]} {parts[1]} {parts[2]} {parts[3]} {parts[5]}"
                # Parse the naive layout string
                report_time = datetime.strptime(
                    clean_date_str, "%a %b %d %H:%M:%S %Y"
                )
                # Explicitly apply timezone awareness to match the new cutoff object
                report_time = report_time.replace(tzinfo=timezone.utc)
            else:
                continue

            if report_time >= cutoff_time:
                # Clean up the name string for local filesystem safety
                safe_name = "".join(
                    c for c in report_name if c.isalnum() or c in (" ", "_", "-")
                ).rstrip()
                safe_name = safe_name.replace(" ", "_")

                # Generate a clean timestamp suffix (e.g., 2026-07-08_223901)
                timestamp_suffix = report_time.strftime("%Y-%m-%d_%H%M%S")
                filename = f"{safe_name}_{timestamp_suffix}.pdf"
                full_output_path = os.path.join(outdir, filename)

                print(
                    f"    -> Downloading: '{report_name}' (Generated: {completion_time_str})"
                )

                download_url = (
                    f"https://{host}/suite-api/api/reports/{report_id}/download"
                )
                download_headers = {
                    "Authorization": f"vRealizeOpsToken {token}",
                    "Accept": "application/pdf",
                }

                with requests.get(
                    download_url,
                    headers=download_headers,
                    stream=True,
                    verify=verify_ssl,
                ) as r:
                    r.raise_for_status()
                    with open(full_output_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                download_count += 1

        except Exception as process_error:
            print(
                f"    [!] Error processing or downloading report {report_id}: {process_error}"
            )

    print(
        f"[+] Done! Successfully processed and saved {download_count} reports to {outdir}"
    )


if __name__ == "__main__":
    params = parse_arguments_and_config()

    verify_ssl = not params["unsafe"]
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        if not os.path.exists(params["outdir"]):
            print(
                f"[!] Warning: Path {params['outdir']} not found. Creating directory..."
            )
            os.makedirs(params["outdir"], exist_ok=True)

        api_token = get_auth_token(
            host=params["host"],
            username=params["user"],
            password=params["password"],
            auth_source=params["authsource"],
            verify_ssl=verify_ssl,
        )

        download_recent_reports(
            host=params["host"],
            token=api_token,
            outdir=params["outdir"],
            verify_ssl=verify_ssl,
        )

    except Exception as e:
        print(f"[CRITICAL ERROR] Script failed: {e}")
        sys.exit(1)
