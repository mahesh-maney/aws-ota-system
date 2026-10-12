import json
import os
import logging
import boto3
import time
from utils import *
import uuid
from boto3.dynamodb.conditions import Attr, Key
import base64
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
import hashlib



# --- AWS Clients ---
_REGION = os.environ.get('REGION', os.environ.get('AWS_REGION', 'ap-south-1'))
iot_client_data = boto3.client('iot-data', region_name=_REGION)
iot_client = boto3.client('iot', region_name=_REGION)
athena = boto3.client('athena', region_name=_REGION)
dynamodb = boto3.resource('dynamodb')
DEVICE_STATE_TABLE = os.environ['DEVICE_STATE_TABLE']
device_table = dynamodb.Table(DEVICE_STATE_TABLE)

# --- Logging ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Environment Variables ---
DATABASE = os.environ['DATABASE']
TABLE = os.environ['TABLE']
SECRET_KEY = os.environ['SECRET_KEY'].encode('utf-8')  # must be 16, 24, or 32 bytes
ATHENA_OUTPUT = f"s3://{os.environ['BUCKET_ATHENA_OUTPUT']}/provisioning_template_function/"

# Thing Group used by the Fleet Provisioning template.
# The provisioning template must declare a ThingGroups parameter and
# reference it from the AWS::IoT::Thing resource.
THING_GROUP = os.environ.get("THING_GROUP", "HON-ZigbeeGateway")


def check_mac_in_athena(mac: str) -> bool:
    """Check if MAC address exists in Athena table."""
    query = f"SELECT mac FROM {DATABASE}.{TABLE} WHERE mac = '{mac}' LIMIT 1"
    logger.info(f"Executing Athena query: {query}")

    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={'Database': DATABASE},
        ResultConfiguration={'OutputLocation': ATHENA_OUTPUT}
    )
    query_execution_id = response['QueryExecutionId']

    # Wait for query to complete
    while True:
        result = athena.get_query_execution(QueryExecutionId=query_execution_id)
        state = result['QueryExecution']['Status']['State']
        if state in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
            break
        time.sleep(0.5)

    if state != 'SUCCEEDED':
        logger.error(f"Athena query failed or cancelled. State: {state}")
        return False

    result = athena.get_query_results(QueryExecutionId=query_execution_id)
    rows = result['ResultSet']['Rows']

    # First row is header. If more than 1 → MAC found
    mac_found = len(rows) > 1
    logger.info(f"MAC {'found' if mac_found else 'not found'} in Athena table.")
    return mac_found


def publish_validation_message(mac, message):
    """Publish validation result to a custom topic so device can receive it."""
    topic = f"things/{mac}/validation/result"

    payload = json.dumps(message)
    logger.info(f"Publishing to topic {topic}: {payload}")

    try:
        iot_client_data.publish(
            topic=topic,
            qos=1,
            payload=payload
        )
    except Exception as e:
        logger.error(f"Failed to publish validation message: {e}")


def lambda_handler(event, context):
    try:
        logger.info(f"Received event: {json.dumps(event)}")
        certificateId = event.get("certificateId")


        if "parameters" not in event:
            logger.error("Unknown payload format")
            return {"error": "Unknown payload format"}

        mac = event["parameters"].get("mac_address")
        logger.info(f"Received MAC address: {mac}")

        if not mac:
            raise ValueError("No mac_address found in parameters")

     
        mac_exists = check_mac_in_athena(mac)

        if not mac_exists:
            msg = {
                "status": 404
            }
            publish_validation_message(mac, msg)
            logger.warning(msg)
            return {
                "allowProvisioning": False,
                "errorMessage": msg
            }

        encrypted_mac = encrypt(mac, SECRET_KEY)
        logger.info(f"encrypted mac: {encrypted_mac}")
        thing_name = "digilux-device"
        table = dynamodb.Table(os.environ.get("TABLE_DEVICE_DATA", "digilux_device_data"))

        existing_item = table.query(
            IndexName="macAddress-index",
            KeyConditionExpression=Key("macAddress").eq(encrypted_mac)
        ).get("Items", [])

        logger.info(f"existing items: {existing_item}")
        if existing_item:

            logger.info(f"MAC {mac} already exists in DynamoDB. Skipping insert.")

            if "deviceId" not in existing_item[0]:
                raise ValueError("deviceId not found in existing_item")

            device_id = existing_item[0]["deviceId"]

            msg =  {
                "status": 409,
                "duId": device_id,
                "topics": [
                    "health",
                    "request",
                    "config",
                    "response",
                    "ota",
                    "scene",
                    "group"
                    ]
            }

            publish_validation_message(mac, msg)
            
            response = make_all_state_values_null(deviceId=device_id)
            logger.info(f"device table response: {response}")

            if response.get("status") == "DELETED":
                logger.info("DeviceId endpoints state data has deleted")
            elif response.get("status") == "NO_STATE_FOUND":
                logger.info("No state present to clear")
            elif response.get("status") == "NO_SUB_DEVICES":
                logger.info("No subDevices found")
            elif response.get("status") == "DEVICE_NOT_FOUND":
                logger.info("Device not found in state table")

            # Make inactive existing [ACTIVE CERTIFICATE]
            existing_active_cert = existing_item[0].get("certificateId")

            if existing_active_cert:
                logger.info(
                    f"Existing certificate found: {existing_active_cert}. "
                    f"Making it INACTIVE.")

                iot_client.update_certificate(
                     certificateId=existing_active_cert, newStatus="INACTIVE")

                logger.info(f"Made existing certificate INACTIVE: {existing_active_cert}")

            else:
                #Need to remove deviceId from logs [NTR]                
                logger.info(
                    f"No certificateId found for deviceId={device_id}. "
                    f"Updating with new certificateId={certificateId}")

            # In both cases, save the NEW certificate ID
            table.update_item(
                    Key={
                    "deviceId": device_id,
                    "macAddress": encrypted_mac
                    },
                    UpdateExpression="SET certificateId = :cert",
                    ExpressionAttributeValues={
                    ":cert": certificateId 
                    })

            # Remove below log [NTR]
            logger.info(f"Updated deviceId={device_id} with new certificateId={certificateId}")

            return {
                "allowProvisioning": True,
                "Message": f"Device with MAC {mac} already exists"
            }

        
        device_id = str(uuid.uuid4())
        data = {
            "deviceId": device_id,
            "macAddress": encrypted_mac,
            "certificateId": certificateId,
            "config": {
                "siteId": "",
                "topics": {
                    "config": f"iot/device/{device_id}/config/#",
                    "request": f"iot/device/{device_id}/request",
                    "response": f"iot/device/{device_id}/response",
                    "health": f"iot/device/{device_id}/health",
                    "scene": f"iot/device/{device_id}/scene",
                    "group": f"iot/device/{device_id}/group",
                    "ota": f"iot/device/{device_id}/ota"
                }
            }
        }
        
        
        table.put_item(Item=data)

        
        # payload = data["config"]["topics"]
        payload = {
            "status": 200,
            "duId": device_id,
            "topics": [
                "health",
                "request",
                "config",
                "response",
                "ota",
                "scene",
                "group"
                ]
        }
        logger.info(f"Topic payload: {payload}")
        publish_validation_message(mac, payload)

        response = {
            "allowProvisioning": True,
            "parameterOverrides": {
                "ThingName": device_id,
                "ThingGroup": THING_GROUP,
                "PolicyName": "provisioning_policy",
                "DecryptedMac": mac
            }
        }

        logger.info(f"Provisioning response: {json.dumps(response)}")
        return response
    
    except Exception:
        logger.exception("DynamoDB update failed")
        raise
        

def encrypt(key, SECRET_KEY):
    # Convert contact number to bytes
    bytes = key.encode('utf-8')

    # Deterministic nonce: derive from SHA256 hash of contact number
    nonce = hashlib.sha256(key.encode()).digest()[:12]

    # Create cipher with deterministic nonce
    cipher = AES.new(SECRET_KEY, AES.MODE_GCM, nonce=nonce)

    # Encrypt deterministically
    ciphertext, tag = cipher.encrypt_and_digest(bytes)

    # Encode final payload for DynamoDB
    payload_encoded = base64.b64encode(nonce + tag + ciphertext).decode('utf-8')
    return payload_encoded

def decrypt(payload_encoded, SECRET_KEY):
    """
    Decrypts the deterministic ciphertext back to the original contact number.
    Requires the same SECRET_KEY and SALT.
    """
    data = base64.b64decode(payload_encoded)

    # Extract nonce, tag, ciphertext
    nonce = data[:12]
    tag = data[12:28]
    ciphertext = data[28:]

    # Rebuild cipher
    cipher = AES.new(SECRET_KEY, AES.MODE_GCM, nonce=nonce)
    decrypted_bytes = cipher.decrypt_and_verify(ciphertext, tag)
    decrypted_text = decrypted_bytes.decode('utf-8')

    return decrypted_text

def make_all_state_values_null(deviceId):
    item = device_table.get_item(
        Key={"deviceId": deviceId}
    ).get("Item")

    if not item:
        logger.info("No device found in device state table!")
        return {"status": "DEVICE_NOT_FOUND"}

    sub_devices = item.get("subDevices", {})
    if not sub_devices:
        return {"status": "NO_SUB_DEVICES"}

    response = device_table.delete_item(
        Key={"deviceId": deviceId},
        ReturnValues="ALL_OLD"
    )

    return {
        "status": "DELETED",
        "deletedItem": response.get("Attributes")
    }
   
