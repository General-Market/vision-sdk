#!/usr/bin/env python3
"""
Vision P2Pool Bot — Joins batches with random bets on HackerNews markets.

Usage:
    python3 bot.py [--once]           # Run once then exit
    python3 bot.py                    # Loop: poll for batches, join new ones

Environment:
    L3_RPC_URL          Chain RPC for Vision (default: http://localhost:8546)
    P2POOL_API_URL      Issuer P2Pool API (default: http://localhost:10001)
    DATA_NODE_URL       Data-node URL (default: http://localhost:8200)
    BOT_ADDRESS         Bot wallet address (must be funded + impersonated on Anvil)
    DEPOSIT_AMOUNT      USDC deposit per batch in whole tokens (default: 10)
    STAKE_PER_TICK      USDC stake per tick in whole tokens (default: 1)
    POLL_INTERVAL       Seconds between poll cycles (default: 30)
"""

import json
import hashlib
import os
import random
import struct
import sys
import time
import logging
from typing import Optional

import requests
from web3 import Web3

# ── Config ──────────────────────────────────────────────────────

L3_RPC_URL = os.environ.get("L3_RPC_URL", "http://localhost:8546")
P2POOL_API_URL = os.environ.get("P2POOL_API_URL", "http://localhost:10001")
DATA_NODE_URL = os.environ.get("DATA_NODE_URL", "http://localhost:8200")
BOT_ADDRESS = os.environ.get("BOT_ADDRESS", "")
BOT_PRIVATE_KEY = os.environ.get("BOT_PRIVATE_KEY", "")
DEPOSIT_AMOUNT = int(os.environ.get("DEPOSIT_AMOUNT", "10"))
STAKE_PER_TICK = int(os.environ.get("STAKE_PER_TICK", "1"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

# ARB_USDC has 6 decimals (Vision now lives on Arbitrum)
DECIMALS = 6
DEPOSIT_WEI = DEPOSIT_AMOUNT * (10 ** DECIMALS)
STAKE_WEI = STAKE_PER_TICK * (10 ** DECIMALS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vision-bot")

# ── ABI fragments ──────────────────────────────────────────────

ERC20_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

VISION_ABI = [
    {
        "name": "joinBatch",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "batchId", "type": "uint256"},
            {"name": "depositAmount", "type": "uint256"},
            {"name": "stakePerTick", "type": "uint256"},
            {"name": "bitmapHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "getPosition",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "batchId", "type": "uint256"},
            {"name": "player", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "bitmapHash", "type": "bytes32"},
                    {"name": "stakePerTick", "type": "uint256"},
                    {"name": "startTick", "type": "uint256"},
                    {"name": "balance", "type": "uint256"},
                    {"name": "lastClaimedTick", "type": "uint256"},
                    {"name": "joinTimestamp", "type": "uint256"},
                    {"name": "totalDeposited", "type": "uint256"},
                    {"name": "totalClaimed", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "name": "registerBot",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "endpoint", "type": "string"},
            {"name": "pubkeyHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "getBatch",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "batchId", "type": "uint256"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "creator", "type": "address"},
                    {"name": "marketIds", "type": "bytes32[]"},
                    {"name": "resolutionTypes", "type": "uint8[]"},
                    {"name": "tickDuration", "type": "uint256"},
                    {"name": "customThresholds", "type": "uint256[]"},
                    {"name": "createdAtTick", "type": "uint256"},
                    {"name": "paused", "type": "bool"},
                ],
            }
        ],
    },
]


# ── Helpers ──────────────────────────────────────────────────────

def load_deployment() -> dict:
    """Load deployment addresses from the active deployment file."""
    paths = [
        "deployments/active-deployment.json",
        "../deployments/active-deployment.json",
        os.path.join(os.path.dirname(__file__), "..", "deployments", "active-deployment.json"),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError("Cannot find active-deployment.json")


def get_addresses() -> tuple[str, str]:
    """Return (Vision address, ARB_USDC address)."""
    deploy = load_deployment()
    return deploy["contracts"]["Vision"], deploy["contracts"]["ARB_USDC"]


def encode_bitmap(bets: list[str], market_count: int) -> bytes:
    """
    Encode UP/DOWN bets into a packed bitmap.
    Bit 1 = UP, Bit 0 = DOWN. Big-endian bit order within each byte.
    """
    byte_count = (market_count + 7) // 8
    bitmap = bytearray(byte_count)
    for i in range(market_count):
        if i < len(bets) and bets[i] == "UP":
            byte_idx = i // 8
            bit_idx = 7 - (i % 8)  # big-endian: bit 0 = MSB
            bitmap[byte_idx] |= 1 << bit_idx
    return bytes(bitmap)


def hash_bitmap(bitmap: bytes) -> bytes:
    """keccak256 hash of the bitmap bytes."""
    return Web3.keccak(bitmap)


def random_bets(n: int) -> list[str]:
    """Generate n random UP/DOWN bets."""
    return [random.choice(["UP", "DOWN"]) for _ in range(n)]


# ── Chain interaction ────────────────────────────────────────────

class VisionBot:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(L3_RPC_URL))
        self.vision_addr, self.usdc_addr = get_addresses()
        self.vision = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.vision_addr),
            abi=VISION_ABI,
        )
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.usdc_addr),
            abi=ERC20_APPROVE_ABI,
        )
        # Derive address from private key
        if not BOT_PRIVATE_KEY:
            raise ValueError("BOT_PRIVATE_KEY env var required")
        self.account = self.w3.eth.account.from_key(BOT_PRIVATE_KEY)
        self.bot_addr = self.account.address
        self.joined_batches: set[int] = set()

    def usdc_balance(self) -> int:
        return self.usdc.functions.balanceOf(self.bot_addr).call()

    def _sign_and_send(self, tx: dict) -> bytes:
        """Sign a transaction with bot private key and send it."""
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return tx_hash

    def approve_usdc(self, amount: int):
        """Approve Vision contract to spend USDC."""
        tx = self.usdc.functions.approve(
            Web3.to_checksum_address(self.vision_addr), amount
        ).build_transaction({
            "from": self.bot_addr,
            "gas": 200_000,
            "gasPrice": self.w3.eth.gas_price,
            "nonce": self.w3.eth.get_transaction_count(self.bot_addr),
        })
        self._sign_and_send(tx)
        log.info("USDC approved: %d", amount)

    def join_batch(self, batch_id: int, deposit: int, stake: int, bitmap_hash: bytes):
        """Call Vision.joinBatch on-chain."""
        tx = self.vision.functions.joinBatch(
            batch_id, deposit, stake, bitmap_hash
        ).build_transaction({
            "from": self.bot_addr,
            "gas": 500_000,
            "gasPrice": self.w3.eth.gas_price,
            "nonce": self.w3.eth.get_transaction_count(self.bot_addr),
        })
        tx_hash = self._sign_and_send(tx)
        log.info("Joined batch %d (tx: %s)", batch_id, tx_hash.hex()[:16])

    def get_position(self, batch_id: int):
        """Read on-chain position for this bot."""
        return self.vision.functions.getPosition(batch_id, self.bot_addr).call()

    def get_batch_info(self, batch_id: int):
        """Read batch info from chain."""
        return self.vision.functions.getBatch(batch_id).call()

    def register_bot(self):
        """Register as a bot on the Vision contract."""
        endpoint = f"http://localhost:9999"  # placeholder
        pubkey_hash = Web3.keccak(text=f"bot-{self.bot_addr}")
        try:
            tx = self.vision.functions.registerBot(
                endpoint, pubkey_hash
            ).build_transaction({
                "from": self.bot_addr,
                "gas": 200_000,
                "gasPrice": self.w3.eth.gas_price,
                "nonce": self.w3.eth.get_transaction_count(self.bot_addr),
            })
            tx_hash = self.w3.eth.send_transaction(tx)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt["status"] == 1:
                log.info("Bot registered on Vision contract")
            else:
                log.warning("Bot registration tx reverted (may already be registered)")
        except Exception as e:
            log.warning("Bot registration failed: %s", e)

    def submit_bitmap_to_issuers(
        self, batch_id: int, bitmap_hex: str, expected_hash: str
    ) -> int:
        """Submit bitmap to issuer nodes. Returns number of acceptances."""
        issuer_urls = [
            "http://localhost:10001",
            "http://localhost:10002",
            "http://localhost:10003",
        ]
        accepted = 0
        for url in issuer_urls:
            try:
                resp = requests.post(
                    f"{url}/p2pool/bitmap",
                    json={
                        "player": self.bot_addr,
                        "batch_id": batch_id,
                        "bitmap_hex": bitmap_hex,
                        "expected_hash": expected_hash,
                    },
                    timeout=10,
                )
                if resp.ok:
                    accepted += 1
            except requests.RequestException:
                pass
        log.info("Bitmap submitted to %d/%d issuers", accepted, len(issuer_urls))
        return accepted

    def fetch_batches(self) -> list[dict]:
        """Fetch active batches from P2Pool API."""
        try:
            resp = requests.get(f"{P2POOL_API_URL}/p2pool/batches", timeout=10)
            if resp.ok:
                data = resp.json()
                return data.get("batches", data if isinstance(data, list) else [])
        except requests.RequestException:
            pass
        return []

    def full_join(self, batch_id: int, market_count: int) -> Optional[dict]:
        """
        Full join flow: generate bets → encode bitmap → approve → join → submit.
        Returns dict with bitmap info or None on failure.
        """
        # Check balance
        balance = self.usdc_balance()
        if balance < DEPOSIT_WEI:
            log.warning(
                "Insufficient USDC: have %d, need %d",
                balance // (10 ** DECIMALS),
                DEPOSIT_AMOUNT,
            )
            return None

        # Generate random bets
        bets = random_bets(market_count)
        bitmap = encode_bitmap(bets, market_count)
        bm_hash = hash_bitmap(bitmap)
        bm_hex = "0x" + bitmap.hex()

        log.info(
            "Batch %d: %d markets, %d UP / %d DOWN",
            batch_id,
            market_count,
            bets.count("UP"),
            bets.count("DOWN"),
        )

        # Approve USDC
        self.approve_usdc(DEPOSIT_WEI)

        # Join on-chain
        self.join_batch(batch_id, DEPOSIT_WEI, STAKE_WEI, bm_hash)

        # Submit bitmap to issuers
        accepted = self.submit_bitmap_to_issuers(
            batch_id, bm_hex, "0x" + bm_hash.hex()
        )

        self.joined_batches.add(batch_id)

        return {
            "batch_id": batch_id,
            "bets": bets,
            "bitmap_hex": bm_hex,
            "bitmap_hash": "0x" + bm_hash.hex(),
            "accepted": accepted,
        }


# ── Main loop ────────────────────────────────────────────────────

def run_once(bot: VisionBot) -> bool:
    """Try to join one batch. Returns True if joined."""
    batches = bot.fetch_batches()
    if not batches:
        log.info("No active batches found")
        return False

    for batch in batches:
        batch_id = batch.get("id", batch.get("batch_id"))
        if batch_id is None:
            continue
        if batch_id in bot.joined_batches:
            continue
        if batch.get("paused"):
            continue

        # Check if already joined on-chain
        try:
            pos = bot.get_position(batch_id)
            if pos[3] > 0:  # balance > 0 means already joined
                log.info("Already joined batch %d (balance: %d)", batch_id, pos[3] // (10 ** DECIMALS))
                bot.joined_batches.add(batch_id)
                continue
        except Exception:
            pass

        # Get market count from chain
        try:
            info = bot.get_batch_info(batch_id)
            market_count = len(info[1])  # marketIds array
        except Exception:
            market_count = batch.get("market_count", 10)

        result = bot.full_join(batch_id, market_count)
        if result:
            # Verify position on-chain
            pos = bot.get_position(batch_id)
            log.info(
                "Position verified — balance: %d USDC, stake/tick: %d USDC",
                pos[3] // (10 ** DECIMALS),
                pos[1] // (10 ** DECIMALS),
            )
            return True

    log.info("No new batches to join")
    return False


def main():
    log.info("Vision P2Pool Bot starting")
    log.info("  Bot address:  %s", BOT_ADDRESS)
    log.info("  L3 RPC:       %s", L3_RPC_URL)
    log.info("  P2Pool API:   %s", P2POOL_API_URL)
    log.info("  Deposit:      %d USDC", DEPOSIT_AMOUNT)
    log.info("  Stake/tick:   %d USDC", STAKE_PER_TICK)

    bot = VisionBot()

    # Check connectivity
    try:
        chain_id = bot.w3.eth.chain_id
        log.info("  Chain ID:     %d", chain_id)
    except Exception as e:
        log.error("Cannot connect to RPC: %s", e)
        sys.exit(1)

    # Check USDC balance
    balance = bot.usdc_balance()
    log.info("  USDC balance: %d", balance // (10 ** DECIMALS))
    if balance < DEPOSIT_WEI:
        log.error("Insufficient USDC balance")
        sys.exit(1)

    # Register as bot
    bot.register_bot()

    once = "--once" in sys.argv

    if once:
        success = run_once(bot)
        sys.exit(0 if success else 1)

    # Poll loop
    while True:
        try:
            run_once(bot)
        except Exception as e:
            log.error("Error in poll cycle: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
