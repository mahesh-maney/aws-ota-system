# provisioning_validation

## AWS Details
| | |
|---|---|
| **Function name** | `provisioning_validation` |
| **Region** | `ap-south-1` |
| **Account** | `986906626244` (Digilux) |
| **Runtime** | Python 3.11 |
| **Last deployed** | 2026-09-28 |
| **Role** | `provisioning_validation-role-0ose72tq` |

## Purpose
IoT Fleet Provisioning pre-hook. Called by AWS IoT when a Zigbee Gateway device
attempts to auto-provision itself via the Fleet Provisioning template.

## Flow
1. Receives MAC address from the provisioning template event
2. Queries Athena (`digilux_data.device_table`) to verify the MAC is approved
3. **Not found** → rejects (`allowProvisioning: false`), publishes 404 to `things/{mac}/validation/result`
4. **Already provisioned** → re-provisioning: deactivates old certificate, updates with new one, publishes 409
5. **New device** → creates `deviceId` (UUID), writes to `digilux_device_data`, publishes 200 with topic list, returns `allowProvisioning: true`

## Files
- `lambda_function.py` — main handler
- `utils.py` — AES-GCM decrypt helper for MAC address

## Known issues
- `SECRET_KEY` stored as plaintext env var — should be moved to Secrets Manager
- Athena query uses string interpolation on MAC address (SQL injection risk)
- No version control history prior to this commit — source was deployed directly on AWS
