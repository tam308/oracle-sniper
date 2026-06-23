import os
import sys
import time
import json
import ctypes
import urllib.request
from datetime import datetime
import oci

def send_notification(message, title="Oracle ARM Sniper"):
    """Push a phone notification via ntfy.sh (or any URL in NOTIFY_URL).
    No-op if NOTIFY_URL isn't set, so local runs are unaffected."""
    url = os.environ.get("NOTIFY_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            method="POST",
            headers={"Title": title, "Priority": "high", "Tags": "tada"},
        )
        urllib.request.urlopen(req, timeout=10)
        print("Notification sent.")
    except Exception as e:
        print(f"Could not send notification: {e}")

# Keep Windows awake while this runs, even if the laptop would normally sleep.
# On CI (Linux) these are no-ops -- the runner never sleeps.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

def prevent_sleep():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        print("Sleep prevention enabled (system will stay awake while running).")
    except Exception as e:
        print(f"Could not enable sleep prevention: {e}")

def allow_sleep():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass

def get_config():
    """Auth from env vars when running in CI (GitHub Actions), else from the
    local ~/.oci/config file. CI sets OCI_USER_OCID etc. as secrets and passes
    the private key contents directly via OCI_KEY_CONTENT."""
    if os.environ.get("OCI_USER_OCID"):
        config = {
            "user": os.environ["OCI_USER_OCID"],
            "tenancy": os.environ["OCI_TENANCY_OCID"],
            "fingerprint": os.environ["OCI_FINGERPRINT"],
            "region": os.environ["OCI_REGION"],
            "key_content": os.environ["OCI_KEY_CONTENT"],
        }
        try:
            oci.config.validate_config(config)
        except Exception as e:
            # validate_config can be picky about key_content vs key_file; the
            # real check happens when the client signs a request. Just warn.
            print(f"Config validation warning: {e}")
        return config
    return oci.config.from_file()

# --- CONFIGURATION ---
COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaaexl2djwij3fgbli4kpwicb2vpyhj6daxpet3zrerbupxvtiis3la"
SUBNET_ID = "ocid1.subnet.oc1.ap-singapore-1.aaaaaaaaz5f4bhkhe7ptoxsvtlolxgnqbbqrnhb7dzohibpr6sx4cm3tmz7q"
AVAILABILITY_DOMAIN = "jyJN:AP-SINGAPORE-1-AD-1"
SSH_PUBLIC_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC3r6xjC4Qfj9tqmiaqKxlbyHlXZoiwcQl3Oqbq4aFSjGz+M/J8ONmXsBUFU4DIi0jH+zK0yNJuUHb5gipcl0RBeEL+rdxDItQ29zdbDSGcgYzTOsV3L2tDQWF9ziL27v/FxHGTfIlnyMj2Z1lmlMV5DyJXI+O1I+3ERCROpKxjyo5A29zPSdIC8hz9n2+A1q9UY+CjVfPA+BWvSaxscxHfvkrNVJbKXZh0N0Tx1zd0/9xnQ90JFvfnJO4AYuUZEkbUDyTkXVHuJERDazUPy2bCnloU2RTrqGoAgjYltV3PEKmP7R+pTSas5Y9uChrtMPbaePGNvS61cOoizpVYMnJx ssh-key-2026-06-22"

# --- ARM AMPERE FLEX SETTINGS ---
# Free tier total is 4 OCPU / 24 GB per region. Set to the minimum (1 OCPU /
# 6 GB) so it fits into whatever A1 quota is left. If you have NO other A1
# instance, bump these back to 4.0 / 24.0 to grab the whole free allocation.
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 1.0
MEMORY_IN_GBS = 6.0
IMAGE_ID = "ocid1.image.oc1.ap-singapore-1.aaaaaaaamynzciw3t7fypsdqahuupzsvbv5ewlubquu3ksfqugxchksgxm4q"

DISPLAY_NAME = "Free-ARM-Ampere-Server"

def instance_already_exists(compute_client):
    """Return True if we already have a non-terminated A1.Flex instance.
    Prevents the cron from launching duplicates every run after we win."""
    try:
        instances = compute_client.list_instances(compartment_id=COMPARTMENT_ID).data
        for inst in instances:
            if inst.shape == SHAPE and inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                print(f"Already have an instance: {inst.display_name} "
                      f"({inst.lifecycle_state}) {inst.id}")
                return True
    except Exception as e:
        # If the check itself fails, don't block launching -- just warn.
        print(f"Could not check existing instances ({type(e).__name__}): {e}")
    return False

def launch_instance(compute_client, fault_domain=None):
    launch_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=COMPARTMENT_ID,
        availability_domain=AVAILABILITY_DOMAIN,
        fault_domain=fault_domain,  # rotate FDs to catch per-FD capacity
        shape=SHAPE,
        # The flexible shape requires this explicit config block:
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_IN_GBS
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=IMAGE_ID
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True
        ),
        metadata={
            "ssh_authorized_keys": SSH_PUBLIC_KEY
        },
        display_name=DISPLAY_NAME
    )

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        print(f"[{ts}] Sending ARM launch request (fault_domain={fault_domain})...")
        response = compute_client.launch_instance(launch_details)
        print("🎉 Success! ARM Ampere instance is now provisioning.")
        print(f"Instance ID: {response.data.id}")
        send_notification(
            f"Got it! ARM Ampere ({OCPUS:g} OCPU / {MEMORY_IN_GBS:g}GB) is provisioning.\n"
            f"Instance ID: {response.data.id}"
        )
        return "success"
    except oci.exceptions.ServiceError as e:
        # Always print the full error so we can tell a capacity miss apart
        # from a real configuration/auth problem.
        print(f"[{ts}] ServiceError -> status={e.status} code={e.code} "
              f"message={e.message!r}")

        # 429 = OCI is rate-limiting our API calls. This is NOT capacity --
        # our request never even reached the capacity check. We must slow down,
        # otherwise we'll just keep getting throttled and never actually snipe.
        if e.status == 429 or e.code == "TooManyRequests":
            print("   -> THROTTLED (rate limit), not a capacity miss. Backing off.")
            return "throttled"

        # The real capacity miss: OCI returns 500/InternalError with this text.
        # This is the signal we're actually waiting for -- everything is correct,
        # there's just no free ARM host right now. Retry at a steady interval.
        if "Out of host capacity" in str(e.message) or "OutOfCapacity" in str(e.code):
            print("   -> Genuine capacity miss. Everything is configured right; retrying.")
            return "retry"

        # Anything else (auth, bad OCID, quota, malformed request) will NEVER
        # succeed by retrying, so stop and surface it instead of wasting time.
        print("   -> This is NOT a capacity error. Stopping so you don't waste time.")
        return "fatal"
    except oci.exceptions.RequestException as e:
        # Transient connectivity problem: dropped Wi-Fi, DNS hiccup, or the
        # laptop briefly waking from sleep. Don't kill an overnight run over a
        # blip -- treat it like a capacity miss and retry.
        print(f"[{ts}] Network error: {e}. Will retry.")
        return "retry"
    except Exception as e:
        # Genuinely unexpected (e.g. missing config file, SDK bug).
        print(f"[{ts}] Unexpected error ({type(e).__name__}): {e}")
        print("   -> Stopping. Fix this before re-running.")
        return "fatal"

if __name__ == "__main__":
    print("Starting Oracle Cloud ARM Ampere capacity sniper loop...")
    prevent_sleep()

    config = get_config()
    compute_client = oci.core.ComputeClient(config)

    # Don't launch a duplicate if we already won on a previous run (matters in
    # CI, where the cron keeps firing). Skip this check with SKIP_EXISTS=1.
    if not os.environ.get("SKIP_EXISTS") and instance_already_exists(compute_client):
        print("Instance already exists -- nothing to do. Exiting.")
        sys.exit(0)

    # We send 1 request per loop, so the loop delay IS our request rate.
    # 60s got 429'd every other try -> the real limit is ~1 request / 120s.
    # 90s keeps us under it while still polling frequently.
    BASE_DELAY = 90              # steady interval between genuine capacity retries
    THROTTLE_DELAY = BASE_DELAY   # current backoff while rate-limited; grows on repeats
    MAX_THROTTLE = 600            # cap the backoff at 10 minutes

    # In CI, bound the run so the job ends and the cron re-triggers it.
    # MAX_RUNTIME_SECONDS unset (local) -> run forever until success/fatal.
    max_runtime = os.environ.get("MAX_RUNTIME_SECONDS")
    max_runtime = int(max_runtime) if max_runtime else None
    start = time.monotonic()

    # Singapore is a single-AD region, so rotate fault domains instead --
    # capacity is evaluated per fault domain, so FD-2 may have room when FD-1 is full.
    FAULT_DOMAINS = ["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"]

    attempt = 0
    try:
        while True:
            if max_runtime and (time.monotonic() - start) >= max_runtime:
                print(f"Reached MAX_RUNTIME_SECONDS={max_runtime}. "
                      "Exiting; the cron will start a fresh run.")
                break

            attempt += 1
            fd = FAULT_DOMAINS[(attempt - 1) % len(FAULT_DOMAINS)]
            print(f"--- Attempt #{attempt} ({fd}) ---")
            result = launch_instance(compute_client, fault_domain=fd)

            if result == "success":
                break

            if result == "fatal":
                print("Aborting loop due to a non-capacity error (see message above).")
                send_notification(
                    "Sniper STOPPED on a non-capacity error (auth/config/quota). "
                    "It is no longer trying -- check the Actions logs.",
                    title="Oracle Sniper STOPPED",
                )
                sys.exit(1)

            if result == "throttled":
                # We're being rate-limited: wait longer each time until it clears,
                # so our requests can actually get through to the capacity check.
                print(f"   Waiting {THROTTLE_DELAY}s to clear the rate limit...")
                time.sleep(THROTTLE_DELAY)
                THROTTLE_DELAY = min(THROTTLE_DELAY * 2, MAX_THROTTLE)
                continue

            # result == "retry": genuine capacity miss -- our request got through.
            # Reset backoff to the BASE interval (not lower) so we don't drift back
            # under the rate limit and start getting throttled again.
            THROTTLE_DELAY = BASE_DELAY
            time.sleep(BASE_DELAY)
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
    finally:
        # Let the machine sleep normally again once we're done.
        allow_sleep()