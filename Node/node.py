import asyncio
import json
import aiofiles

import websockets
import aiohttp
import socket

import os
import random

from Crypto.Signature import DSS
from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC

# Configuration Variables
entrypoints = ["ws://qwhwdauhdasht.ddns.net:6969"]  # List of known nodes that can be used to "enter" the network.
ledgerDir = "Accounts/"  # Path to the directory where the ledger will be stored (must end in /)
publicFile = "public_key.pem"  # Path of the node's public key
privateFile = "private_key.pem"  # Path of the node's private key
consensusPercent = 0.65  # Float representing what percent of the online voting nodes must agree with a transaction for it to be confirmed.

# Import node's private key
f = open(privateFile, "rt")
privateKey = ECC.import_key(f.read())
f.close()

# Import node's private key
f = open(publicFile, "rt")
publicKey = ECC.import_key(f.read())
f.close()

# Get a nicer representation of the public key
publicKeyStr = publicKey.export_key(format="PEM", compress=True)
publicKeyStr = publicKeyStr.replace("-----BEGIN PUBLIC KEY-----\n", "")
publicKeyStr = publicKeyStr.replace("\n-----END PUBLIC KEY-----", "")
publicKeyStr = publicKeyStr.replace("\n", " ")

# Create the ledger directory
os.makedirs(ledgerDir, exist_ok=True)

# Initialise global variables
nodes = {}  # Dictionary of all connected nodes. Structure follows:
            #   {ip : websocket}
            #     ip - str - The IP/hostname that the node can be reached by. Includes port and "ws://" prefix
            #     websocket - websockets.Websocket - the websocket that the node can be reached on.

sendSubscriptions = {}  # Dictionary of nodes that should be alerted when an account is referenced in a send transaction. Structure follows:
                        #   {address : [websocket,...]}
                        #     address - str - MXC address that is being monitored
                        #     websocket - websockets.Websocket - the websocket that the node can be reached on.

votePool = {}   # Dictionary of all ongoing votes. Structure follows:
                  # {voteID : [consensusWeight, block, [address,...]]}
                    # voteID - Float - Randomly generated ID for each voting round.
                    # consensusWeight - Float - Represents the value that the total voted weight must exceed for a block to be confirmed.
                    # address - Str - The MXC address of the voting node.
                    # voteWeight - Float - The voting weight of the voting node. Negative if voting against.
ip = -1
myPort = -1


async def balance(data: dict) -> str:
    """Returns an account's balance"""

    address = data["address"]

    try:
        block = await getHead(address)

    except FileNotFoundError:
        response = f'{{"type": "rejection", "address": "{address}", "reason": "addressNonExistent"}}'
        return response

    response = f'{{"type": "info", "address": "{address}", "balance": "{block["balance"]}" }}'
    return response


async def broadcast(data):
    """Broadcast a verified transaction to other nodes"""

    broadcastID = str(random.randint(0, 99999999999999999999))
    broadcastID = "0"*(20-len(broadcastID)) + broadcastID

    validNodesStr = ""
    validNodes = []
    for node in nodes:
        ws = nodes[node]
        try:
            await ws.send('{"type": "ping"}')
            resp = await ws.recv()
            if json.loads(resp)["type"] == "confirm":
                print("Available", node)
                validNodesStr = validNodesStr + "|" + node
                validNodes.append(node)

        except:
            pass

    packet = {"type": "vote", "voteID": broadcastID, "block": data, "address": publicKeyStr}
    signature = await genSignature(json.dumps(packet), privateKey)
    packet["signature"] = signature
    for node in validNodes:
        await nodes[node].send(json.dumps(packet))
        try:
            resp = await asyncio.wait_for(nodes[node].recv(), 5)
            resp = json.loads(resp)
            if resp["type"] != "confirm":
                raise Exception(f"Invalid response: {json.dumps(resp)}")

            print("Vote received by ", node)

        except TimeoutError:
            print("Vote not received by ", node)

        except Exception as e:
            print("Exception while receiving vote response")
            print(e)


# Return any send transactions that have not been received by an account
async def checkForPendingSend(data):
    address = data["address"]

    received = []
    accounts = os.listdir(ledgerDir)
    if address in accounts:
        f = await aiofiles.open(ledgerDir + address)
        data = await f.read()
        await f.close()

        data = data.splitlines()
        for block in data:
            block = json.loads(block)
            if block["type"] == "receive":
                received.append(block["link"])

            if block["type"] == "open":
                received.append(block["link"])

    for i in accounts:
        f = await aiofiles.open(f"{ledgerDir}{i}")
        blocks = await f.read()
        await f.close()
        blocks = blocks.splitlines()
        for block in blocks:
            block = json.loads(block)
            if f'{block["address"]}/{block["id"]}' in received:
                continue

            if block["type"] == "send":
                if block["link"] == address:
                    amount = await getBlock(block["address"], block["previous"])
                    amount = float(amount["balance"]) - float(block["balance"])

                    resp = {"type": "pendingSend", "link": f"{block['address']}/{block['id']}", "sendAmount": amount}
                    resp = json.dumps(resp)
                    return resp

    response = {"type": "pendingSend", "link": "", "sendAmount": ""}
    return json.dumps(response)


# Return a list of available nodes
async def fetchNodes():
    global nodes
    nodeAddresses = ""
    for node in nodes:
        nodeAddresses = nodeAddresses + "|" + node

    response = {"type": "confirm", "action": "fetchNodes", "nodes": nodeAddresses}
    return json.dumps(response)


async def genSignature(data, privateKey):
    """ Sign data with private key"""

    data = json.dumps(data)
    signer = DSS.new(privateKey, "deterministic-rfc6979")
    signatureHash = SHA256.new()
    signatureHash.update(data.encode("utf-8"))
    signature = signer.sign(signatureHash)
    signature = hex(int.from_bytes(signature, "little"))

    return signature


# Return a block belonging to the account (address) with block ID (blockID)
async def getBlock(address, blockID):
    f = await aiofiles.open(f"{ledgerDir}{address}")
    fileStr = await f.read()
    await f.close()
    fileStr = fileStr.splitlines()

    blocks = []
    for block in fileStr:
        blocks.append(json.loads(block))

    for block in blocks:
        if block["id"] == blockID:
            return block

    print("not found")


# Get the head block of an account (the most recent block)
async def getHead(address):
    f = await aiofiles.open(f"{ledgerDir}{address}")
    fileStr = await f.read()
    await f.close()
    fileStr = fileStr.splitlines()

    blocks = []
    for block in fileStr:
        blocks.append(json.loads(block))

    if len(blocks) == 1:
        return blocks[0]

    # Sort blocks in order
    isSorted = False
    while not isSorted:
        isSorted = True
        for i in range(1, len(blocks)):
            previous = blocks[i]["previous"]
            if previous == "0"*20:  # if broken, change to 145
                blocks.insert(0, blocks.pop(i))
                continue

            if blocks[i-1]["id"] == previous:
                continue

            isSorted = False

            for x in range(len(blocks)):
                if blocks[x]["id"] == previous:
                    blocks.insert(x+1, blocks.pop(i))
                    break

    return blocks[-1]


# Process an open transaction
async def openAccount(data):
    signature = data["signature"]
    address = data["address"]
    blockID = data["id"]

    valid = await verifySignature(signature, address, data)
    if not valid:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "signature"}}'
        return toRespond

    sendingAddress, sendingBlock = data["link"].split("/")
    sendingBlock = await getBlock(sendingAddress, sendingBlock)

    # Check that send block is valid
    valid = await verifySignature(sendingBlock["signature"], sendingAddress, sendingBlock)
    if not valid:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "sendSignature"}}'
        return toRespond

    previousBlock = await getBlock(sendingAddress, sendingBlock["previous"])
    sendAmount = float(previousBlock["balance"]) - float(sendingBlock["balance"])

    if float(data["balance"]) != float(sendAmount):
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "invalidBalance"}}'
        return toRespond

    if data["previous"] != "0"*20:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "invalidPrevious"}}'
        return toRespond

    toRespond = f'{{"type": "confirm", "address": "{address}", "id": "{blockID}"}}'
    return toRespond


# Process a receive transaction
async def receive(data):
    signature = data["signature"]
    address = data["address"]
    blockID = data["id"]

    valid = await verifySignature(signature, address, data)
    if not valid:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "signature"}}'
        return toRespond

    sendingAddress, sendingBlock = data["link"].split("/")
    sendingBlock = await getBlock(sendingAddress, sendingBlock)

    # Check that send block is valid
    valid = await verifySignature(sendingBlock["signature"], sendingAddress, sendingBlock)
    if not valid:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "sendSignature"}}'
        return toRespond

    f = await aiofiles.open(f"{ledgerDir}{address}")
    blocks = await f.read()
    await f.close()
    blocks = blocks.splitlines()
    for block in blocks:
        if json.loads(block)["type"] == "genesis":
            continue

        if json.loads(block)["link"] == data["link"]:
            response = {"type": "rejection", "address": address, "id": blockID, "reason": "doubleReceive"}
            return json.dumps(response)

    previousBlock = await getBlock(sendingAddress, sendingBlock["previous"])
    sendAmount = float(previousBlock["balance"]) - float(sendingBlock["balance"])

    head = await getHead(address)
    if float(data["balance"]) != float(head["balance"]) + float(sendAmount):
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "invalidBalance"}}'
        return toRespond

    if data["previous"] != head["id"]:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "invalidPrevious"}}'
        return toRespond

    toRespond = f'{{"type": "confirm", "address": "{address}", "id": "{blockID}"}}'
    return toRespond


# Register myself with specified node
async def registerMyself(node):
    global myPort
    global ip
    print(f"Registering with {node}")
    websocket = await websockets.connect(node)
    await websocket.send(f'{{"type": "registerNode", "port": "{myPort}"}}')
    resp = await websocket.recv()
    if json.loads(resp)["type"] == "confirm":
        print(f"Node registered with: {node}")
        global nodes
        nodes = {**nodes, **{node: websocket}}

        await websocket.send('{"type": "fetchNodes"}')
        newNodes = await websocket.recv()
        print(newNodes)
        newNodes = json.loads(newNodes)["nodes"].split("|")[1:]
        print(newNodes)
        for node in newNodes:
            nodeIP = node.replace("ws://", "").split(":")[0]
            print(nodeIP)
            print(myPort)
            print(node.split(":")[2])
            isLocalMachine = (nodeIP == "localhost" or nodeIP == "127.0.0.1" or nodeIP == ip) and str(myPort) == str(node.split(":")[2])
            if node not in nodes and not isLocalMachine:
                await registerMyself(node)

    else:
        await websocket.close()
        print(f"Failed to register with: {node}")


# Processes a send transaction
async def send(data):
    signature = data["signature"]
    address = data["address"]
    blockID = data["id"]

    valid = await verifySignature(signature, address, data)
    if not valid:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "signature"}}'
        return toRespond

    head = await getHead(address)
    if float(head["balance"]) < float(data["balance"]):
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "balance"}}'
        return toRespond

    if head["id"] != data["previous"]:
        toRespond = f'{{"type": "rejection", "address": "{address}", "id": "{blockID}", "reason": "invalidPrevious"}}'
        return toRespond

    toRespond = f'{{"type": "confirm", "address": "{address}", "id": "{blockID}"}}'
    if data["link"] in sendSubscriptions:
        sendAlert = {"type": "sendAlert", "address": data["link"],
                     "sendAmount": str(float(head["balance"]) - float(data["balance"])),
                     "link": f"{address}/{blockID}"}

        sendAlert = json.dumps(sendAlert)
        for ws in sendSubscriptions[data["link"]]:
            try:
                await ws.send(sendAlert)

            except websockets.exceptions.ConnectionClosedError:
                pass

    return toRespond


# Verifies that data was created by stated account
async def verifySignature(signature, publicKey, data):
    data = data.copy()
    data.pop("signature")
    data = json.dumps(data)
    publicKey = publicKey.split(" ")
    publicKey = "-----BEGIN PUBLIC KEY-----\n" + publicKey[0] + "\n" + publicKey[1] + "\n-----END PUBLIC KEY-----"
    publicKey = ECC.import_key(publicKey)
    signature = (int(signature, 16)).to_bytes(64, byteorder="little")
    data = SHA256.new(data.encode("utf-8"))
    verifier = DSS.new(publicKey, "fips-186-3")
    try:
        verifier.verify(data, signature)
        return True

    except ValueError:
        return False


# Verify given block in dictionary accounts recursively
async def verifyBlock(accounts, block, usedAsPrevious=[]):
    if accounts[block["address"]][block["id"]][1]:
        return True

    # I need to clarify why this isn't "if not". "not None" returns True, but in this case I'm using the value None to represent a block which has no status yet and False to represent a block which has been rejected
    if accounts[block["address"]][block["id"]][1] == False:
        return False

    # check if previous block is already cited as previous by another block
    if (block["address"] + "/" + block["previous"]) in usedAsPrevious:
        accounts[block["address"]][block["id"]][1] = False
        print("previous already used")
        return False

    # verify signature
    validSig = await verifySignature(block["signature"], block["address"], block)
    if not validSig:
        accounts[block["address"]][block["id"]][1] = False
        print("invalid signature")
        return False

    if block["type"] != "open" and block["type"] != "genesis":
        prevBlock = await getBlock(block["address"], block["previous"])

    # if send block, verify previous block balance is more than current balance
    if block["type"] == "send":
        if float(prevBlock["balance"]) < float(block["balance"]):
            accounts[block["address"]][block["id"]][1] = False
            print("New balance is too large")
            return False

    # if receive/open block, calculate the send amount and check if the new balance matches
    if block["type"] in ["receive", "open"]:
        sendBlock = await getBlock(block["link"].split("/")[0], block["link"].split("/")[1])
        sendPrevious = await getBlock(sendBlock["address"], sendBlock["previous"])
        sendAmount = float(sendPrevious["balance"]) - float(sendBlock["balance"])

        previousBalance = 0
        if block["type"] == "receive":
            previousBalance = float(prevBlock["balance"])

        if block["balance"] != previousBalance + sendAmount:
            accounts[block["address"]][block["id"]][1] = False
            print("new balance mismatch")
            return False

        if not await verifyBlock(accounts, sendBlock, usedAsPrevious):
            accounts[block["address"]][block["id"]][1] = False
            print("invalid send block")
            return False

    if block["type"] == "genesis":
        if block["signature"] != "0xc9052f33ef7690bf24171ec5c4f506caeee1ab88419dc6abc0644e6033f6c526ccff87f6bc8096b0463e38e3221c054b88938408fbaada4a6148d46d38daa52b":
            accounts[block["address"]][block["id"]][1] = False
            print("FAKE GENESIS DETECTED")
            return False

    if block["type"] == "open":
        accounts[block["address"]][block["id"]][1] = True
        print("Open Block Verified")
        return True

    if block["type"] == "genesis":
        accounts[block["address"]][block["id"]][1] = True
        print("Genesis Block Verified")
        return True

    if await verifyBlock(accounts, prevBlock, usedAsPrevious):
        accounts[block["address"]][block["id"]][1] = True
        print("Block Verified")
        toReturn = True

    else:
        accounts[block["address"]][block["id"]][1] = False
        print("Previous block is invalid")
        toReturn = False

    usedAsPrevious.append(block["address"] + "/" + prevBlock["id"])
    return toReturn

# Verifies EVERY transaction in the ledger (should probably only be called after downloading the ledger)
async def verifyLedger():
    accounts = {}
    accountsDir = os.listdir(ledgerDir)
    for account in accountsDir:
        f = await aiofiles.open(ledgerDir + account)
        data = await f.read()
        await f.close()

        blocks = {}
        data = data.splitlines()
        for block in data:
            print(block)
            block = json.loads(block)
            blocks[block["id"]] = [block, None]

        accounts[account] = blocks

    accountNames = accounts.keys()
    for accountName in accountNames:
        for block in accounts[accountName]:
            block = accounts[accountName][block][0]
            if accounts[accountName][block["id"]][1] == None:
                await verifyBlock(accounts, block)

    accountNames = accounts.keys()
    for accountName in accountNames:
        for block in accounts[accountName]:
            if not accounts[accountName][block][1]:
                print("BLOCK NOT VALID!!!!!")

    print("Ledger Verified!")


async def vote(data):
    """ Called when receiving a vote.
        1 - Validates block.
        2 - Adds to local vote pool, even if invalid.
        3 - If valid, transmit my vote to all nodes I am in contact with."""

    data = json.loads(data)
    blockType = json.loads(data["block"])["type"]
    if blockType == "send":
        resp = await send(data)

    elif blockType == "receive":
        resp = await receive(data)

    elif blockType == "open":
        resp = await open(data)

    else:
        print(f"Invalid Block Type: {blockType}")
        return

    if json.loads(resp)["type"] == "confirm":
        valid = True
        print(f"Incoming vote block is valid: {data['block']}")

    else:
        valid = False
        print(f"Incoming vote block is invalid: {data['block']}")


async def watchForSends(data, ws):
    """ Allows a connection to receive notifications when a given address is sent new MXC.
        Not persistently stored, needs to be re-called if node restarts."""

    global sendSubscriptions
    address = data["address"]

    try:
        prevSubs = sendSubscriptions[address]

    except KeyError:
        prevSubs = []

    prevSubs.append(ws)
    sendSubscriptions[address] = prevSubs

    resp = {"type": "confirm", "action": "watchForSends", "address": address}
    return json.dumps(resp)


# Handles incoming websocket connections
async def incoming(websocket, path):
    global nodes
    print(f"Client Connected: {websocket.remote_address[0]}")
    while True:
        try:
            data = await websocket.recv()

        except:
            print("Client Disconnected")
            break

        print(data)
        data = json.loads(data)
        if data["type"] == "ping":
            response = '{"type": "confirm", "action": "ping"}'

        elif data["type"] == "balance":
            response = await balance(data)

        elif data["type"] == "send":
            response = await send(data)
            if json.loads(response)["type"] == "confirm":
                f = await aiofiles.open(f"{ledgerDir}{data['address']}", "a")
                await f.write("\n" + json.dumps(data))
                await f.close()

        elif data["type"] == "pendingSend":
            response = await checkForPendingSend(data)

        elif data["type"] == "receive":
            response = await receive(data)
            if json.loads(response)["type"] == "confirm":
                f = await aiofiles.open(f"{ledgerDir}{data['address']}", "a")
                await f.write("\n" + json.dumps(data))
                await f.close()

        elif data["type"] == "open":
            response = await openAccount(data)
            if json.loads(response)["type"] == "confirm":
                f = await aiofiles.open(f"{ledgerDir}{data['address']}", "a+")
                await f.write(json.dumps(data))
                await f.close()

        elif data["type"] == "getPrevious":
            head = await getHead(data["address"])
            address = data["address"]
            previous = head["id"]
            response = f'{{"type": "previous", "address": "{address}", "link": "{previous}"}}'

        elif data["type"] == "registerNode":
            response = json.dumps({"type": "confirm", "action": "registerNode"})
            nodes = {**nodes, **{f"ws://{websocket.remote_address[0]}:{data['port']}": websocket}}

        elif data["type"] == "fetchNodes":
            response = await fetchNodes()

        elif data["type"] == "watchForSends":
            response = await watchForSends(data, websocket)

        else:
            response = f'{{"type": "rejection", "reason": "unknown request"}}'

        await websocket.send(response)


# Handles incoming ledger requests
async def ledgerServer(websocket, url):
    print("Ledger Requested")
    for account in os.listdir(ledgerDir):
        await websocket.send(f"Account:{account}")
        f = await aiofiles.open(ledgerDir + account)
        toSend = await f.read()
        await f.close()
        for line in toSend.splitlines():
            await websocket.send(line)

    await websocket.send("ayothatsall")


# Fetches the ledger from the specified node
async def fetchLedger(node):
    node = node.split(":")[0] + ":" + node.split(":")[1] + ":" + str(int(node.split(":")[2])+1)
    print(node)
    websocket = await websockets.connect(node)

    accounts = {}
    msg = await websocket.recv()
    while msg != "ayothatsall":
        curAccount = msg.replace("Account:", "")
        accounts[curAccount] = []
        msg = await websocket.recv()
        while "Account:" not in msg and "ayothatsall" not in msg:
            accounts[curAccount].append(msg)
            msg = await websocket.recv()

    for account in accounts:
        toWrite = ""
        for block in accounts[account]:
            toWrite = toWrite + "\n" + block

        toWrite = toWrite.replace("\n", "", 1)
        f = await aiofiles.open(ledgerDir + account, "w+")
        await f.write(toWrite)
        await f.close()


# Check if node running on given url
async def testWebsocket(url):
    try:
        websocket = await asyncio.wait_for(websockets.connect(url), 3)
        await websocket.send('{"type": "ping"}')
        await websocket.recv()
        await websocket.close()

        return True

    except:
        return False


# Starts the node
async def run():
    global ip
    global myPort
    # Get my public IP
    async with aiohttp.ClientSession() as session:
        async with session.get('https://api.ipify.org') as response:
            ip = await response.text()

    if await testWebsocket(f"ws://{ip}:6969"):
        # A node already exists on our network, so boot on the secondary port
        await websockets.serve(incoming, "0.0.0.0", 5858)
        myPort = 5858
        entrypoints.append(f"ws://{ip}:6969")

    else:
        # No other nodes exist on our network, so boot on the primary port
        await websockets.serve(incoming, "0.0.0.0", 6969)
        myPort = 6969

    for node in entrypoints:
        if not await testWebsocket(node):
            print(f"Node not available: {node}")
            continue

        nodeIP = node.replace("ws://", "").split(":")[0]
        nodeIP = socket.gethostbyname(nodeIP)
        isLocalMachine = (nodeIP == "localhost" or nodeIP == "127.0.0.1" or nodeIP == ip) and str(myPort) == str(node.split(":")[2])
        if isLocalMachine:
            print(f"I am that node!")
            continue

        await registerMyself(node)

    print(nodes)
    if len(list(nodes.keys())) != 0:
        await fetchLedger(random.choice(list(nodes.keys())))

    await verifyLedger()

    print(f"Booting on {ip}:{myPort}")
    await websockets.serve(ledgerServer, "0.0.0.0", myPort+1)
    await asyncio.Event().wait()

asyncio.run(run())
