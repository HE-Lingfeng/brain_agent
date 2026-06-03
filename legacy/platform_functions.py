#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorldQuant BRAIN MCP Server - Python Version
A comprehensive Model Context Protocol (MCP) server for WorldQuant BRAIN platform integration.
"""

# Ensure proper encoding handling for Windows
import sys
import os

# Note: We'll handle encoding issues in individual functions rather than
# overriding system streams to avoid conflicts with MCP server

import json
import time
import asyncio
import logging
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import math
from time import sleep

import requests
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, EmailStr

from pathlib import Path


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Pydantic models for type safety
class AuthCredentials(BaseModel):
    email: EmailStr
    password: str

class SimulationSettings(BaseModel):
    instrumentType: str = "EQUITY"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: float = 0.0
    neutralization: str = "NONE"
    truncation: float = 0.0
    pasteurization: str = "ON"
    unitHandling: str = "VERIFY"
    nanHandling: str = "OFF"
    language: str = "FASTEXPR"
    visualization: bool = True
    testPeriod: str = "P0Y0M"
    selectionHandling: str = "POSITIVE"
    selectionLimit: int = 1000
    maxTrade: str = "OFF"
    componentActivation: str = "IS"

class SimulationData(BaseModel):
    type: str = "REGULAR"  # "REGULAR" or "SUPER"
    settings: SimulationSettings
    regular: Optional[str] = None
    combo: Optional[str] = None
    selection: Optional[str] = None

class BrainApiClient:
    """WorldQuant BRAIN API client with comprehensive functionality."""
    
    def __init__(self):
        self.base_url = "https://api.worldquantbrain.com"
        self.session = requests.Session()
        self.auth_credentials = None
        self.is_authenticating = False
        
        # Configure session
        self.session.timeout = 30
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def log(self, message: str, level: str = "INFO"):
        """Log messages to stderr to avoid MCP protocol interference."""
        try:
            # Try to print with original message first
            print(f"[{level}] {message}", file=sys.stderr)
        except UnicodeEncodeError:
            # Fallback: remove problematic characters and try again
            try:
                safe_message = message.encode('ascii', 'ignore').decode('ascii')
                print(f"[{level}] {safe_message}", file=sys.stderr)
            except Exception:
                # Final fallback: just print the level and a safe message
                print(f"[{level}] Log message", file=sys.stderr)
        except Exception:
            # Final fallback: just print the level and a safe message
            print(f"[{level}] Log message", file=sys.stderr)
    
    async def authenticate(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate with WorldQuant BRAIN platform with biometric support."""
        self.log("🔐 Starting Authentication process...", "INFO")
        
        try:
            # Store credentials for potential re-authentication
            self.auth_credentials = {'email': email, 'password': password}
            
            # Clear any existing session data
            self.session.cookies.clear()
            self.session.auth = None
            
            # Create Basic Authentication header (base64 encoded credentials)
            import base64
            credentials = f"{email}:{password}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            
            # Send POST request with Basic Authentication header
            headers = {
                'Authorization': f'Basic {encoded_credentials}'
            }
            
            response = self.session.post('https://api.worldquantbrain.com/authentication', headers=headers)
            
            # Check for successful authentication (status code 201)
            if response.status_code == 201:
                self.log("Authentication successful", "SUCCESS")
                
                # Check if JWT token was automatically stored by session
                jwt_token = self.session.cookies.get('t')
                if jwt_token:
                    self.log("JWT token automatically stored by session", "SUCCESS")
                else:
                    self.log("⚠️ No JWT token found in session", "WARNING")
                
                # Return success response
                return {
                    'user': {'email': email},
                    'status': 'authenticated',
                    'permissions': ['read', 'write'],
                    'message': 'Authentication successful',
                    'status_code': response.status_code,
                    'has_jwt': jwt_token is not None
                }
            
            # Check if biometric authentication is required (401 with persona)
            elif response.status_code == 401:
                www_auth = response.headers.get("WWW-Authenticate")
                location = response.headers.get("Location")
                
                if www_auth == "persona" and location:
                    self.log("🔴 Biometric authentication required", "INFO")
                    
                    # Handle biometric authentication
                    from urllib.parse import urljoin
                    biometric_url = urljoin(response.url, location)
                    
                    return await self._handle_biometric_auth(biometric_url, email)
                else:
                    raise Exception("Incorrect email or password")
            else:
                raise Exception(f"Authentication failed with status code: {response.status_code}")
                    
        except requests.HTTPError as e:
            self.log(f"❌ HTTP error during authentication: {e}", "ERROR")
            raise
        except Exception as e:
            self.log(f"❌ Authentication failed: {str(e)}", "ERROR")
            raise
    
    async def _handle_biometric_auth(self, biometric_url: str, email: str) -> Dict[str, Any]:
        """Handle biometric authentication using browser automation."""
        self.log("🌐 Starting biometric authentication...", "INFO")
        
        try:
            # Import selenium for browser automation
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            import time
            
            # Setup Chrome options
            options = Options()
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            
            driver = None
            try:
                # Open browser with timeout
                driver = webdriver.Chrome(options=options)
                # Set a short timeout so it doesn't wait forever
                driver.set_page_load_timeout(80)  # Only wait 5 seconds
                
                self.log("🌐 Opening browser for biometric authentication...", "INFO")
                
                # Try to open the URL but handle timeout
                try:
                    driver.get(biometric_url)
                    self.log("Browser page loaded successfully", "SUCCESS")
                except Exception as timeout_error:
                    self.log(f"⚠️ Page load timeout (expected): {str(timeout_error)[:50]}...", "WARNING")
                    self.log("Browser window is open for biometric authentication", "INFO")
                
                # Print instructions
                print("\n" + "="*60, file=sys.stderr)
                print("BIOMETRIC AUTHENTICATION REQUIRED", file=sys.stderr)
                print("="*60, file=sys.stderr)
                print("Browser window is open with biometric authentication page", file=sys.stderr)
                print("Complete the biometric authentication in the browser", file=sys.stderr)
                print("The system will automatically check when you're done...", file=sys.stderr)
                print("="*60, file=sys.stderr)
                
                # Keep checking until authentication is complete
                max_attempts = 60  # 5 minutes maximum (60 * 5 seconds)
                attempt = 0
                
                while attempt < max_attempts:
                    time.sleep(5)  # Check every 5 seconds
                    attempt += 1
                    
                    # Check if authentication completed
                    check_response = self.session.post(biometric_url)
                    self.log(f"🔄 Checking authentication status (attempt {attempt}/{max_attempts}): {check_response.status_code}", "INFO")
                    
                    if check_response.status_code == 201:
                        self.log("Biometric authentication successful!", "SUCCESS")
                        
                        # Close browser
                        driver.quit()
                        
                        # Check JWT token
                        jwt_token = self.session.cookies.get('t')
                        if jwt_token:
                            self.log("JWT token received", "SUCCESS")
                        
                        # Return success response
                        return {
                            'user': {'email': email},
                            'status': 'authenticated',
                            'permissions': ['read', 'write'],
                            'message': 'Biometric authentication successful',
                            'status_code': check_response.status_code,
                            'has_jwt': jwt_token is not None
                        }
                
                # If we get here, authentication timed out
                if driver:
                    driver.quit()
                raise Exception("Biometric authentication timed out")
                
            except Exception as driver_error:
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
                raise Exception(f"Browser automation error: {driver_error}")
                
        except Exception as e:
            self.log(f"❌ Biometric authentication failed: {str(e)}", "ERROR")
            raise
    
    async def is_authenticated(self) -> bool:
        """Check if currently authenticated using JWT token."""
        try:
            # Check if we have a JWT token in cookies
            jwt_token = self.session.cookies.get('t')
            if not jwt_token:
                self.log("❌ No JWT token found", "INFO")
                return False
            
            # Test authentication with a simple API call
            response = self.session.get(f"{self.base_url}/authentication")
            if response.status_code == 200:
                return True
            elif response.status_code == 401:
                self.log("❌ JWT token expired or invalid (401)", "INFO")
                return False
            else:
                self.log(f"⚠️ Unexpected status code during auth check: {response.status_code}", "WARNING")
                return False
        except Exception as e:
            self.log(f"❌ Error checking authentication: {str(e)}", "ERROR")
            return False
    
    async def ensure_authenticated(self):
        """Ensure authentication is valid, re-authenticate if needed."""
        if not await self.is_authenticated() and self.auth_credentials:
            self.log("🔄 Re-authenticating...", "INFO")
            await self.authenticate(self.auth_credentials['email'], self.auth_credentials['password'])
        elif not self.auth_credentials:
            raise Exception("Not authenticated and no stored credentials available. Please call authenticate() first.")
    
    async def get_authentication_status(self) -> Optional[Dict[str, Any]]:
        """Get current authentication status and user info."""
        try:
            response = self.session.get(f"{self.base_url}/users/self")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get auth status: {str(e)}", "ERROR")
            return None
    
    async def create_simulation(self, simulation_data: SimulationData) -> Dict[str, str]:
        """Create a new simulation on BRAIN platform."""
        await self.ensure_authenticated()
        
        try:
            self.log("🚀 Creating simulation...", "INFO")
            
            # Prepare settings based on simulation type
            settings_dict = simulation_data.settings.dict()
            
            # Remove fields based on simulation type
            if simulation_data.type == "REGULAR":
                # Remove SUPER-specific fields for REGULAR
                settings_dict.pop('selectionHandling', None)
                settings_dict.pop('selectionLimit', None)
                settings_dict.pop('componentActivation', None)
            elif simulation_data.type == "SUPER":
                # SUPER type keeps all fields
                pass
            
            # Filter out None values from settings
            settings_dict = {k: v for k, v in settings_dict.items() if v is not None}
            
            # Prepare simulation payload
            payload = {
                'type': simulation_data.type,
                'settings': settings_dict
            }
            
            # Add type-specific fields
            if simulation_data.type == "REGULAR":
                if simulation_data.regular:
                    payload['regular'] = simulation_data.regular
            elif simulation_data.type == "SUPER":
                if simulation_data.combo:
                    payload['combo'] = simulation_data.combo
                if simulation_data.selection:
                    payload['selection'] = simulation_data.selection
            
            # Filter out None values from entire payload
            payload = {k: v for k, v in payload.items() if v is not None}
            
            # Debug: print payload for troubleshooting
            # print("📋 Sending payload:")
            # print(json.dumps(payload, indent=2))
            
            response = self.session.post(f"{self.base_url}/simulations", json=payload)
            response.raise_for_status()
            
            # Handle empty response body - extract simulation ID from Location header
            location = response.headers.get('Location', '')
            simulation_id = location.split('/')[-1] if location else None
            
            self.log(f"Simulation created with ID: {simulation_id}", "SUCCESS")


            finished = False
            while True:
                simulation_progress = self.session.get(location)
                if simulation_progress.headers.get("Retry-After", 0) == 0:
                    break
                print("Sleeping for " + simulation_progress.headers["Retry-After"] + " seconds")
                sleep(float(simulation_progress.headers["Retry-After"]))
            print("Alpha done simulating, getting alpha details")
            alpha_id = simulation_progress.json()["alpha"]
            alpha = self.session.get("https://api.worldquantbrain.com/alphas/" + alpha_id)
            result = alpha.json()
            result['note'] = "if you got a negative alpha sharpe, you can just add a minus sign in front of the last line of the Alpha to flip then think the next step."
            return result
            
        except Exception as e:
            self.log(f"❌ Failed to create simulation: {str(e)}", "ERROR")
            raise
    
    # get_simulation_status function removed as requested
    # wait_for_simulation function removed as requested
    
    async def get_alpha_details(self, alpha_id: str) -> Dict[str, Any]:
        """Get detailed information about an alpha."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/alphas/{alpha_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get alpha details: {str(e)}", "ERROR")
            raise

    def _is_atom(self, detail: Optional[Dict[str, Any]]) -> bool:
        """Match atom detection used in extract_regular_alphas.py:
        - Primary signal: 'classifications' entries containing 'SINGLE_DATA_SET'
        - Fallbacks: tags list contains 'atom' or classification id/name contains 'ATOM'
        """
        if not detail or not isinstance(detail, dict):
            return False

        classifications = detail.get('classifications') or []
        for c in classifications:
            cid = (c.get('id') or c.get('name') or '')
            if isinstance(cid, str) and 'SINGLE_DATA_SET' in cid:
                return True

        # Fallbacks
        tags = detail.get('tags') or []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str) and t.strip().lower() == 'atom':
                    return True

        for c in classifications:
            cid = (c.get('id') or c.get('name') or '')
            if isinstance(cid, str) and 'ATOM' in cid.upper():
                return True

        return False
    
    async def get_datasets(self, instrument_type: str = "EQUITY", region: str = "USA", 
                          delay: int = 1, universe: str = "TOP3000", theme: str = "ALL", search: Optional[str] = None) -> Dict[str, Any]:
        """Get available datasets."""
        await self.ensure_authenticated()
        
        try:
            params = {
                'instrumentType': instrument_type,
                'region': region,
                'delay': delay,
                'universe': universe,
                'theme': theme
            }
            
            if search:
                params['search'] = search
            
            response = self.session.get(f"{self.base_url}/data-sets", params=params)
            response.raise_for_status()
            response = response.json()
            response['extraNote'] = "if your returned result is 0, you may want to check your parameter by using get_platform_setting_options tool to got correct parameter"
            return response
        except Exception as e:
            self.log(f"Failed to get datasets: {str(e)}", "ERROR")
            raise
    
    async def get_datafields(self, instrument_type: str = "EQUITY", region: str = "USA",
                            delay: int = 1, universe: str = "TOP3000", theme: str = "false",
                            dataset_id: Optional[str] = None, data_type: str = "",
                            search: Optional[str] = None) -> Dict[str, Any]:
        """Get available data fields."""
        await self.ensure_authenticated()
        
        try:
            params = {
                'instrumentType': instrument_type,
                'region': region,
                'delay': delay,
                'universe': universe,
                'limit': '50',
                'offset': '0'
            }
            
            if data_type != 'ALL':
                params['type'] = data_type
            
            if dataset_id:
                params['dataset.id'] = dataset_id
            if search:
                params['search'] = search
            
            response = self.session.get(f"{self.base_url}/data-fields", params=params)
            response.raise_for_status()
            response = response.json()
            response['extraNote'] = "if your returned result is 0, you may want to check your parameter by using get_platform_setting_options tool to got correct parameter"
            return response
        except Exception as e:
            self.log(f"Failed to get datafields: {str(e)}", "ERROR")
            raise
    
    async def get_alpha_pnl(self, alpha_id: str) -> Dict[str, Any]:
        """Get PnL data for an alpha with retry logic."""
        await self.ensure_authenticated()
        
        max_retries = 5
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get PnL for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/recordsets/pnl")
                response.raise_for_status()
                
                # Some alphas may return 204 No Content or an empty body
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        self.log(f"Empty PnL response for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5  # Exponential backoff
                        continue
                    else:
                        self.log(f"Empty PnL response after {max_retries} attempts for {alpha_id}", "WARNING")
                        return {}
                
                try:
                    pnl_data = response.json()
                    if pnl_data:
                        self.log(f"Successfully retrieved PnL data for alpha {alpha_id}", "SUCCESS")
                        return pnl_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty PnL JSON for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5
                            continue
                        else:
                            self.log(f"Empty PnL JSON after {max_retries} attempts for {alpha_id}", "WARNING")
                            return {}
                            
                except Exception as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"PnL JSON parse failed for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        self.log(f"PnL JSON parse failed for {alpha_id} after {max_retries} attempts: {parse_err}", "WARNING")
                        return {}
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get alpha PnL for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds: {str(e)}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    self.log(f"Failed to get alpha PnL for {alpha_id} after {max_retries} attempts: {str(e)}", "ERROR")
                    raise
        
        # This should never be reached, but just in case
        return {}
    
    async def get_user_alphas(
        self,
        stage: str = "OS",
        limit: int = 30,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        submission_start_date: Optional[str] = None,
        submission_end_date: Optional[str] = None,
        order: Optional[str] = None,
        hidden: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Get user's alphas with advanced filtering."""
        await self.ensure_authenticated()
        
        try:
            params = {
                "stage": stage,
                "limit": limit,
                "offset": offset,
            }
            if start_date:
                params["dateCreated>"] = start_date
            if end_date:
                params["dateCreated<"] = end_date
            if submission_start_date:
                params["dateSubmitted>"] = submission_start_date
            if submission_end_date:
                params["dateSubmitted<"] = submission_end_date
            if order:
                params["order"] = order
            if hidden is not None:
                params["hidden"] = str(hidden).lower()

            response = self.session.get(f"{self.base_url}/users/self/alphas", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user alphas: {str(e)}", "ERROR")
            raise
    
    async def submit_alpha(self, alpha_id: str) -> bool:
        """Submit an alpha for production."""
        await self.ensure_authenticated()
        
        try:
            self.log(f"📤 Submitting alpha {alpha_id} for production...", "INFO")
            
            response = self.session.post(f"{self.base_url}/alphas/{alpha_id}/submit")
            response.raise_for_status()
            
            self.log(f"Alpha {alpha_id} submitted successfully", "SUCCESS")
            return response.__dict__
            
        except Exception as e:
            self.log(f"❌ Failed to submit alpha: {str(e)}", "ERROR")
            return False
    
    async def get_events(self) -> Dict[str, Any]:
        """Get available events and competitions."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/events")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get events: {str(e)}", "ERROR")
            raise
    
    async def get_leaderboard(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get leaderboard data."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            
            if user_id:
                params['user'] = user_id
            else:
                # Get current user ID if not specified
                user_response = self.session.get(f"{self.base_url}/users/self")
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    params['user'] = user_data.get('id')
            
            response = self.session.get(f"{self.base_url}/consultant/boards/leader", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get leaderboard: {str(e)}", "ERROR")
            raise

    async def get_operators(self) -> Dict[str, Any]:
        """Get available operators for alpha creation."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/operators")
            response.raise_for_status()
            operators_data = response.json()
            
            # Ensure we return a dictionary format even if API returns a list
            if isinstance(operators_data, list):
                return {"operators": operators_data, "count": len(operators_data)}
            else:
                return operators_data
        except Exception as e:
            self.log(f"Failed to get operators: {str(e)}", "ERROR")
            raise

    async def run_selection(
        self,
        selection: str,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        selection_limit: int = 1000,
        selection_handling: str = "POSITIVE"
    ) -> Dict[str, Any]:
        """Run a selection query to filter instruments."""
        await self.ensure_authenticated()
        
        try:
            selection_data = {
                "selection": selection,
                "instrumentType": instrument_type,
                "region": region,
                "delay": delay,
                "selectionLimit": selection_limit,
                "selectionHandling": selection_handling
            }
            
            response = self.session.get(f"{self.base_url}/simulations/super-selection", params=selection_data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to run selection: {str(e)}", "ERROR")
            raise

    async def get_user_profile(self, user_id: str = "self") -> Dict[str, Any]:
        """Get user profile information."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/users/{user_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user profile: {str(e)}", "ERROR")
            raise

    async def get_documentations(self) -> Dict[str, Any]:
        """Get available documentations and learning materials."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/tutorials")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get documentations: {str(e)}", "ERROR")
            raise

    # get_messages_summary function removed as requested

    async def get_messages(self, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
        """Get messages for the current user with optional pagination.

        Image / large binary payload mitigation:
          Some messages embed base64 encoded images (e.g. <img src="data:image/png;base64,..."/>).
          Returning full base64 can explode token usage for an LLM client. We post-process each
          message description and (by default) extract embedded base64 images to disk and replace
          them with lightweight placeholders while preserving context.

        Strategies (environment driven in future – currently parameterless public API):
          - placeholder (default): save images to message_images/ and replace with marker text.
          - ignore: strip image tags entirely, leaving a note.
          - keep: leave description unchanged (unsafe for LLM token limits).

        A message dict gains an 'extracted_images' list when images are processed.
        """
        await self.ensure_authenticated()

        import re, base64, pathlib

        image_handling = os.environ.get("BRAIN_MESSAGE_IMAGE_MODE", "placeholder").lower()
        save_dir = pathlib.Path("message_images")

        from typing import Tuple
        def process_description(desc: str, message_id: str) -> Tuple[str, List[str]]:
            try:
                if not desc or image_handling == "keep":
                    return desc, []
                attachments: List[str] = []
                # Regex to capture full <img ...> tag with data URI
                img_tag_pattern = re.compile(r"<img[^>]+src=\"(data:image/[^\"]+)\"[^>]*>", re.IGNORECASE)
                # Iterate over unique matches to avoid double work
                matches = list(img_tag_pattern.finditer(desc))
                if not matches:
                    # Additional heuristic: very long base64-looking token inside quotes followed by </img>
                    # (legacy format noted by user sample). Replace with placeholder.
                    heuristic_pattern = re.compile(r"([A-Za-z0-9+/]{500,}={0,2})\"\s*</img>")
                    if image_handling != "keep" and heuristic_pattern.search(desc):
                        placeholder = "[Embedded image removed - large base64 sequence truncated]"
                        return heuristic_pattern.sub(placeholder + "</img>", desc), []
                    return desc, []

                # Ensure save directory exists only if we will store something
                if image_handling == "placeholder" and not save_dir.exists():
                    try:
                        save_dir.mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        self.log(f"Could not create image save directory: {e}", "WARNING")

                new_desc = desc
                for idx, match in enumerate(matches, start=1):
                    data_uri = match.group(1)  # data:image/...;base64,XXXX
                    if not data_uri.lower().startswith("data:image"):
                        continue
                    # Split header and base64 payload
                    if "," not in data_uri:
                        continue
                    header, b64_data = data_uri.split(",", 1)
                    mime_part = header.split(";")[0]  # data:image/png
                    ext = "png"
                    if "/" in mime_part:
                        ext = mime_part.split("/")[1]
                    safe_ext = (ext or "img").split("?")[0]
                    placeholder_text = "[Embedded image]"
                    if image_handling == "ignore":
                        replacement = f"[Image removed: {safe_ext}]"
                    elif image_handling == "placeholder":
                        # Try decode & save
                        file_name = f"{message_id}_{idx}.{safe_ext}"
                        file_path = save_dir / file_name
                        try:
                            # Guard extremely large strings (>5MB ~ 6.7M base64 chars) to avoid memory blow
                            if len(b64_data) > 7_000_000:
                                raise ValueError("Image too large to decode safely")
                            with open(file_path, "wb") as f:
                                f.write(base64.b64decode(b64_data))
                            attachments.append(str(file_path))
                            replacement = f"[Image extracted -> {file_path}]"
                        except Exception as e:
                            self.log(f"Failed to decode embedded image in message {message_id}: {e}", "WARNING")
                            replacement = "[Image extraction failed - content omitted]"
                    else:  # keep
                        replacement = placeholder_text  # shouldn't be used since early return, but safe
                    # Replace only the matched tag (not global) – use re.sub with count=1 on substring slice
                    # Safer to operate on new_desc using the exact matched string
                    original_tag = match.group(0)
                    new_desc = new_desc.replace(original_tag, replacement, 1)
                return new_desc, attachments
            except UnicodeEncodeError as ue:
                self.log(f"Unicode encoding error in process_description: {ue}", "WARNING")
                return desc, []
            except Exception as e:
                self.log(f"Error in process_description: {e}", "WARNING")
                return desc, []

        try:
            params = {}
            if limit is not None:
                params['limit'] = limit
            if offset > 0:
                params['offset'] = offset

            response = self.session.get(f"{self.base_url}/users/self/messages", params=params)
            response.raise_for_status()
            data = response.json()

            # Post-process results for image handling
            results = data.get('results', [])
            for msg in results:
                try:
                    desc = msg.get('description')
                    processed_desc, attachments = process_description(desc, msg.get('id', 'msg'))
                    if attachments or desc != processed_desc:
                        msg['description'] = processed_desc
                        if attachments:
                            msg['extracted_images'] = attachments
                        else:
                            # If changed but no attachments (ignore mode) mark sanitized
                            msg['sanitized'] = True
                except UnicodeEncodeError as ue:
                    self.log(f"Unicode encoding error sanitizing message {msg.get('id')}: {ue}", "WARNING")
                    # Keep original description if encoding fails
                    continue
                except Exception as inner_e:
                    self.log(f"Failed to sanitize message {msg.get('id')}: {inner_e}", "WARNING")
            data['results'] = results
            data['image_handling'] = image_handling
            return data
        except UnicodeEncodeError as ue:
            self.log(f"Failed to get messages due to encoding error: {str(ue)}", "ERROR")
            raise
        except Exception as e:
            self.log(f"Failed to get messages: {str(e)}", "ERROR")
            raise

    async def get_glossary_terms(self, email: str, password: str, headless: bool = False) -> Dict[str, Any]:
        """Get glossary terms from forum."""
        try:
            # Import and use forum functions
            from forum_functions import forum_client
            return await forum_client.get_glossary_terms(email, password, headless)
        except ImportError:
            self.log("Forum functions not available - install selenium and run forum_functions.py", "WARNING")
            return {"error": "Forum functions require selenium. Use forum_functions.py directly."}
        except Exception as e:
            self.log(f"Glossary extraction failed: {str(e)}", "ERROR")
            return {"error": str(e)}

    async def search_forum_posts(self, email: str, password: str, search_query: str, 
                               max_results: int = 50, headless: bool = True) -> Dict[str, Any]:
        """Search forum posts."""
        try:
            # Import and use forum functions
            from forum_functions import forum_client
            return await forum_client.search_forum_posts(email, password, search_query, max_results, headless)
        except ImportError:
            self.log("Forum functions not available - install selenium and run forum_functions.py", "WARNING")
            return {"error": "Forum functions require selenium. Use forum_functions.py directly."}
        except Exception as e:
            self.log(f"Forum search failed: {str(e)}", "ERROR")
            return {"error": str(e)}

    async def read_forum_post(self, email: str, password: str, article_id: str, 
                           headless: bool = False) -> Dict[str, Any]:
        """Get forum post."""
        try:
            # Import and use forum functions
            from forum_functions import forum_client
            return await forum_client.read_full_forum_post(email, password, article_id, headless, include_comments=True)
        except ImportError:
            self.log("Forum functions not available - install selenium and run forum_functions.py", "WARNING")
            return {"error": "Forum functions require selenium. Use forum_functions.py directly."}
        except Exception as e:
            self.log(f"Forum post retrieval failed: {str(e)}", "ERROR")
            return {"error": str(e)}

    async def get_alpha_yearly_stats(self, alpha_id: str) -> Dict[str, Any]:
        """Get yearly statistics for an alpha with retry logic."""
        await self.ensure_authenticated()
        
        max_retries = 5
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get yearly stats for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/recordsets/yearly-stats")
                response.raise_for_status()
                
                # Check if response has content
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        self.log(f"Empty yearly stats response for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5  # Exponential backoff
                        continue
                    else:
                        self.log(f"Empty yearly stats response after {max_retries} attempts for {alpha_id}", "WARNING")
                        return {}
                
                try:
                    yearly_stats = response.json()
                    if yearly_stats:
                        self.log(f"Successfully retrieved yearly stats for alpha {alpha_id}", "SUCCESS")
                        return yearly_stats
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty yearly stats JSON for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5
                            continue
                        else:
                            self.log(f"Empty yearly stats JSON after {max_retries} attempts for {alpha_id}", "WARNING")
                            return {}
                            
                except Exception as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"Yearly stats JSON parse failed for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        self.log(f"Yearly stats JSON parse failed for {alpha_id} after {max_retries} attempts: {parse_err}", "WARNING")
                        return {}
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get alpha yearly stats for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds: {str(e)}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    self.log(f"Failed to get alpha yearly stats for {alpha_id} after {max_retries} attempts: {str(e)}", "ERROR")
                    raise
        
        # This should never be reached, but just in case
        return {}

    async def get_production_correlation(self, alpha_id: str) -> Dict[str, Any]:
        """Get production correlation data for an alpha with retry logic."""
        await self.ensure_authenticated()
        
        max_retries = 5
        retry_delay = 20  # seconds
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get production correlation for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/correlations/prod")
                response.raise_for_status()
                
                # Check if response has content
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        self.log(f"Empty production correlation response for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        self.log(f"Empty production correlation response after {max_retries} attempts for {alpha_id}", "WARNING")
                        return {}
                
                try:
                    correlation_data = response.json()
                    if correlation_data:
                        self.log(f"Successfully retrieved production correlation for alpha {alpha_id}", "SUCCESS")
                        return correlation_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty production correlation JSON for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            self.log(f"Empty production correlation JSON after {max_retries} attempts for {alpha_id}", "WARNING")
                            return {}
                            
                except Exception as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"Production correlation JSON parse failed for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        self.log(f"Production correlation JSON parse failed for {alpha_id} after {max_retries} attempts: {parse_err}", "WARNING")
                        return {}
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get production correlation for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds: {str(e)}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    self.log(f"Failed to get production correlation for {alpha_id} after {max_retries} attempts: {str(e)}", "ERROR")
                    raise
        
        # This should never be reached, but just in case
        return {}

    async def get_self_correlation(self, alpha_id: str) -> Dict[str, Any]:
        """Get self-correlation data for an alpha with retry logic."""
        await self.ensure_authenticated()
        
        max_retries = 5
        retry_delay = 20  # seconds
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get self correlation for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/correlations/self")
                response.raise_for_status()
                
                # Check if response has content
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        self.log(f"Empty self correlation response for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        self.log(f"Empty self correlation response after {max_retries} attempts for {alpha_id}", "WARNING")
                        return {}
                
                try:
                    correlation_data = response.json()
                    if correlation_data:
                        self.log(f"Successfully retrieved self correlation for alpha {alpha_id}", "SUCCESS")
                        return correlation_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty self correlation JSON for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            self.log(f"Empty self correlation JSON after {max_retries} attempts for {alpha_id}", "WARNING")
                            return {}
                            
                except Exception as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"Self correlation JSON parse failed for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        self.log(f"Self correlation JSON parse failed for {alpha_id} after {max_retries} attempts: {parse_err}", "WARNING")
                        return {}
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get self correlation for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds: {str(e)}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    self.log(f"Failed to get self correlation for {alpha_id} after {max_retries} attempts: {str(e)}", "ERROR")
                    raise
        
        # This should never be reached, but just in case
        return {}

    async def check_correlation(self, alpha_id: str, correlation_type: str = "both", threshold: float = 0.7) -> Dict[str, Any]:
        """Check alpha correlation against production alphas, self alphas, or both."""
        await self.ensure_authenticated()
        
        try:
            results = {
                'alpha_id': alpha_id,
                'threshold': threshold,
                'correlation_type': correlation_type,
                'checks': {}
            }
            
            # Determine which correlations to check
            check_types = []
            if correlation_type == "both":
                check_types = ["production", "self"]
            else:
                check_types = [correlation_type]
            
            all_passed = True
            
            for check_type in check_types:
                if check_type == "production":
                    correlation_data = await self.get_production_correlation(alpha_id)
                elif check_type == "self":
                    correlation_data = await self.get_self_correlation(alpha_id)
                else:
                    continue
                
                # Analyze correlation data (robust to schema/records format)
                if isinstance(correlation_data, dict):
                    # Prefer strict access to schema.max or top-level max; otherwise error
                    schema = correlation_data.get('schema') or {}
                    if isinstance(schema, dict) and 'max' in schema:
                        max_correlation = float(schema['max'])
                    elif 'max' in correlation_data:
                        # Some endpoints place max at top-level
                        max_correlation = float(correlation_data['max'])
                    else:
                        # Attempt to derive from records; if none found, raise error instead of defaulting
                        records = correlation_data.get('records') or []
                        if isinstance(records, list) and records:
                            candidate_max = None
                            for row in records:
                                if isinstance(row, (list, tuple)):
                                    for v in row:
                                        try:
                                            vf = float(v)
                                            if -1.0 <= vf <= 1.0:
                                                candidate_max = vf if candidate_max is None else max(candidate_max, vf)
                                        except Exception:
                                            continue
                                elif isinstance(row, dict):
                                    for key in ('correlation', 'prodCorrelation', 'selfCorrelation', 'max'):
                                        try:
                                            vf = float(row.get(key))
                                            if -1.0 <= vf <= 1.0:
                                                candidate_max = vf if candidate_max is None else max(candidate_max, vf)
                                        except Exception:
                                            continue
                            if candidate_max is None:
                                raise ValueError("Unable to derive max correlation from records")
                            max_correlation = float(candidate_max)
                        else:
                            raise KeyError("Correlation response missing 'schema.max' or top-level 'max' and no 'records' to derive from")
                else:
                    raise TypeError("Correlation data is not a dictionary")

                passes_check = max_correlation < threshold
                
                results['checks'][check_type] = {
                    'max_correlation': max_correlation,
                    'passes_check': passes_check,
                    'correlation_data': correlation_data
                }
                
                if not passes_check:
                    all_passed = False
            
            results['all_passed'] = all_passed
            
            return results
            
        except Exception as e:
            self.log(f"Failed to check correlation: {str(e)}", "ERROR")
            raise

    async def get_submission_check(self, alpha_id: str) -> Dict[str, Any]:
        """Comprehensive pre-submission check."""
        await self.ensure_authenticated()
        
        try:
            # Get correlation checks using the unified function
            correlation_checks = await self.check_correlation(alpha_id, correlation_type="both")
            
            # Get alpha details for additional validation
            alpha_details = await self.get_alpha_details(alpha_id)
            
            # Compile comprehensive check results
            checks = {
                'correlation_checks': correlation_checks,
                'alpha_details': alpha_details,
                'all_passed': correlation_checks['all_passed']
            }
            
            return checks
        except Exception as e:
            self.log(f"Failed to get submission check: {str(e)}", "ERROR")
            raise

    async def set_alpha_properties(self, alpha_id: str, name: Optional[str] = None, 
                                 color: Optional[str] = None, tags: List[str] = None,
                                 selection_desc: str = "None", combo_desc: str = "None") -> Dict[str, Any]:
        """Update alpha properties (name, color, tags, descriptions)."""
        await self.ensure_authenticated()
        
        try:
            data = {}
            if name:
                data['name'] = name
            if color:
                data['color'] = color
            if tags:
                data['tags'] = tags
            if selection_desc:
                data['selectionDesc'] = selection_desc
            if combo_desc:
                data['comboDesc'] = combo_desc
            
            response = self.session.patch(f"{self.base_url}/alphas/{alpha_id}", json=data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to set alpha properties: {str(e)}", "ERROR")
            raise

    async def get_record_sets(self, alpha_id: str) -> Dict[str, Any]:
        """List available record sets for an alpha."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/recordsets")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get record sets: {str(e)}", "ERROR")
            raise

    async def get_record_set_data(self, alpha_id: str, record_set_name: str) -> Dict[str, Any]:
        """Get data from a specific record set."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/recordsets/{record_set_name}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get record set data: {str(e)}", "ERROR")
            raise

    async def get_user_activities(self, user_id: str, grouping: Optional[str] = None) -> Dict[str, Any]:
        """Get user activity diversity data."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            if grouping:
                params['grouping'] = grouping
            
            response = self.session.get(f"{self.base_url}/users/{user_id}/activities", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user activities: {str(e)}", "ERROR")
            raise

    async def get_pyramid_multipliers(self) -> Dict[str, Any]:
        """Get current pyramid multipliers showing BRAIN's encouragement levels."""
        await self.ensure_authenticated()
        
        try:
            # Use the correct endpoint without parameters
            response = self.session.get(f"{self.base_url}/users/self/activities/pyramid-multipliers")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get pyramid multipliers: {str(e)}", "ERROR")
            raise

    async def value_factor_trendScore(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Compute diversity score for regular alphas in a date range.

        Description:
        This function calculate the diversity of the users' submission, by checking the diversity, we can have a good understanding on the valuefactor's trend.
        value factor of a user is defiend by This diversity score, which measures three key aspects of work output: the proportion of works
        with the "Atom" tag (S_A), atom proportion, the breadth of pyramids covered (S_P), and how evenly works
        are distributed across those pyramids (S_H). Calculated as their product, it rewards
        strong performance across all three dimensions—encouraging more Atom-tagged works,
        wider pyramid coverage, and balanced distribution—with weaknesses in any area lowering
        the total score significantly.

        Inputs (hints for AI callers):
        - start_date (str): ISO UTC start datetime, e.g. '2025-08-14T00:00:00Z'
        - end_date (str): ISO UTC end datetime, e.g. '2025-08-18T23:59:59Z'
        - Note: this tool always uses 'OS' (submission dates) to define the window; callers do not need to supply a stage.
                - Note: P_max (total number of possible pyramids) is derived from the platform
                    pyramid-multipliers endpoint and not supplied by callers.

        Returns (compact JSON): {
            'diversity_score': float,
            'N': int,  # total regular alphas in window
            'A': int,  # number of Atom-tagged works (is_single_data_set)
            'P': int,  # pyramid coverage count in the sample
            'P_max': int, # used max for normalization
            'S_A': float, 'S_P': float, 'S_H': float,
            'per_pyramid_counts': {pyramid_name: count}
        }
        """
        # Fetch user alphas (always use OS / submission dates per product policy)
        await self.ensure_authenticated()
        alphas_resp = await self.get_user_alphas(stage='OS', limit=500, submission_start_date=start_date, submission_end_date=end_date)

        if not isinstance(alphas_resp, dict) or 'results' not in alphas_resp:
            return {'error': 'Unexpected response from get_user_alphas', 'raw': alphas_resp}

        alphas = alphas_resp['results']
        regular = [a for a in alphas if a.get('type') == 'REGULAR']

        # Fetch details for each regular alpha
        pyramid_list = []
        atom_count = 0
        per_pyramid = {}
        for a in regular:
            try:
                detail = await self.get_alpha_details(a.get('id'))
            except Exception:
                continue

            is_atom = self._is_atom(detail)
            if is_atom:
                atom_count += 1

            # Extract pyramids
            ps = []
            if isinstance(detail.get('pyramids'), list):
                ps = [p.get('name') for p in detail.get('pyramids') if p.get('name')]
            else:
                pt = detail.get('pyramidThemes') or {}
                pss = pt.get('pyramids') if isinstance(pt, dict) else None
                if pss and isinstance(pss, list):
                    ps = [p.get('name') for p in pss if p.get('name')]

            for p in ps:
                pyramid_list.append(p)
                per_pyramid[p] = per_pyramid.get(p, 0) + 1

        N = len(regular)
        A = atom_count
        P = len(per_pyramid)

        # Determine P_max similarly to the script: use pyramid multipliers if available
        P_max = None
        try:
            pm = await self.get_pyramid_multipliers()
            if isinstance(pm, dict) and 'pyramids' in pm:
                pyramids_list = pm.get('pyramids') or []
                P_max = len(pyramids_list)
        except Exception:
            P_max = None

        if not P_max or P_max <= 0:
            P_max = max(P, 1)

        # Component scores
        S_A = (A / N) if N > 0 else 0.0
        S_P = (P / P_max) if P_max > 0 else 0.0

        # Entropy
        S_H = 0.0
        if P <= 1 or not per_pyramid:
            S_H = 0.0
        else:
            total_occ = sum(per_pyramid.values())
            H = 0.0
            for cnt in per_pyramid.values():
                q = cnt / total_occ if total_occ > 0 else 0
                if q > 0:
                    H -= q * math.log2(q)
            max_H = math.log2(P) if P > 0 else 1
            S_H = (H / max_H) if max_H > 0 else 0.0

        diversity_score = S_A * S_P * S_H

        return {
            'diversity_score': diversity_score,
            'N': N,
            'A': A,
            'P': P,
            'P_max': P_max,
            'S_A': S_A,
            'S_P': S_P,
            'S_H': S_H,
            'per_pyramid_counts': per_pyramid
        }

    async def get_pyramid_alphas(self, start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Dict[str, Any]:
        """Get user's current alpha distribution across pyramid categories."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            if start_date:
                params['startDate'] = start_date
            if end_date:
                params['endDate'] = end_date
            
            # Try the user-specific activities endpoint first (like pyramid-multipliers)
            response = self.session.get(f"{self.base_url}/users/self/activities/pyramid-alphas", params=params)
            
            # If that fails, try alternative endpoints
            if response.status_code == 404:
                # Try alternative endpoint structure
                response = self.session.get(f"{self.base_url}/users/self/pyramid/alphas", params=params)
                
                if response.status_code == 404:
                    # Try yet another alternative
                    response = self.session.get(f"{self.base_url}/activities/pyramid-alphas", params=params)
                    
                    if response.status_code == 404:
                        # Return an informative error with what we tried
                        return {
                            "error": "Pyramid alphas endpoint not found",
                            "tried_endpoints": [
                                "/users/self/activities/pyramid-alphas",
                                "/users/self/pyramid/alphas", 
                                "/activities/pyramid-alphas",
                                "/pyramid/alphas"
                            ],
                            "suggestion": "This endpoint may not be available in the current API version"
                        }
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get pyramid alphas: {str(e)}", "ERROR")
            raise

    async def get_user_competitions(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get list of competitions that the user is participating in."""
        await self.ensure_authenticated()
        
        try:
            if not user_id:
                # Get current user ID if not specified
                user_response = self.session.get(f"{self.base_url}/users/self")
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    user_id = user_data.get('id')
                else:
                    user_id = 'self'
            
            response = self.session.get(f"{self.base_url}/users/{user_id}/competitions")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user competitions: {str(e)}", "ERROR")
            raise

    async def get_competition_details(self, competition_id: str) -> Dict[str, Any]:
        """Get detailed information about a specific competition."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/competitions/{competition_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get competition details: {str(e)}", "ERROR")
            raise

    async def get_competition_agreement(self, competition_id: str) -> Dict[str, Any]:
        """Get the rules, terms, and agreement for a specific competition."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/competitions/{competition_id}/agreement")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get competition agreement: {str(e)}", "ERROR")
            raise

    async def get_platform_setting_options(self) -> Dict[str, Any]:
        """Get available instrument types, regions, delays, and universes."""
        await self.ensure_authenticated()
        
        try:
            # Use OPTIONS method on simulations endpoint to get configuration options
            response = self.session.options(f"{self.base_url}/simulations")
            response.raise_for_status()
            
            # Parse the settings structure from the response
            settings_data = response.json()
            settings_options = settings_data['actions']['POST']['settings']['children']
            
            # Extract instrument configuration options
            instrument_type_data = {}
            region_data = {}
            universe_data = {}
            delay_data = {}
            neutralization_data = {}
            
            # Parse each setting type
            for key, setting in settings_options.items():
                if setting['type'] == 'choice':
                    if setting['label'] == 'Instrument type':
                        instrument_type_data = setting['choices']
                    elif setting['label'] == 'Region':
                        region_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Universe':
                        universe_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Delay':
                        delay_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Neutralization':
                        neutralization_data = setting['choices']['instrumentType']
            
            # Build comprehensive instrument options
            data_list = []
            
            for instrument_type in instrument_type_data:
                for region in region_data[instrument_type['value']]:
                    for delay in delay_data[instrument_type['value']]['region'][region['value']]:
                        row = {
                            'InstrumentType': instrument_type['value'],
                            'Region': region['value'],
                            'Delay': delay['value']
                        }
                        row['Universe'] = [
                            item['value'] for item in universe_data[instrument_type['value']]['region'][region['value']]
                        ]
                        row['Neutralization'] = [
                            item['value'] for item in neutralization_data[instrument_type['value']]['region'][region['value']]
                        ]
                        data_list.append(row)
            
            # Return structured data
            return {
                'instrument_options': data_list,
                'total_combinations': len(data_list),
                'instrument_types': [item['value'] for item in instrument_type_data],
                'regions_by_type': {
                    item['value']: [r['value'] for r in region_data[item['value']]]
                    for item in instrument_type_data
                }
            }
            
        except Exception as e:
            self.log(f"Failed to get instrument options: {str(e)}", "ERROR")
            raise

    async def performance_comparison(self, alpha_id: str, team_id: Optional[str] = None, 
                                   competition: Optional[str] = None) -> Dict[str, Any]:
        """Get performance comparison data for an alpha."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            if team_id:
                params['team_id'] = team_id
            if competition:
                params['competition'] = competition
            
            response = self.session.get(f"{self.base_url}/alphas/{alpha_id}/performance-comparison", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get performance comparison: {str(e)}", "ERROR")
            raise

    # combine_test_results function removed as requested

    async def expand_nested_data(self, data: List[Dict[str, Any]], preserve_original: bool = True) -> List[Dict[str, Any]]:
        """Flatten complex nested data structures into tabular format."""
        try:
            expanded_data = []
            
            for item in data:
                expanded_item = {}
                
                for key, value in item.items():
                    if isinstance(value, dict):
                        # Expand nested dictionary
                        for nested_key, nested_value in value.items():
                            expanded_key = f"{key}_{nested_key}"
                            expanded_item[expanded_key] = nested_value
                        
                        # Preserve original if requested
                        if preserve_original:
                            expanded_item[key] = value
                    elif isinstance(value, list):
                        # Handle list values
                        expanded_item[key] = str(value) if value else []
                        
                        # Preserve original if requested
                        if preserve_original:
                            expanded_item[key] = value
                    else:
                        # Simple value
                        expanded_item[key] = value
                
                expanded_data.append(expanded_item)
            
            return expanded_data
        except Exception as e:
            self.log(f"Failed to expand nested data: {str(e)}", "ERROR")
            raise

    # generate_alpha_links function removed as requested

    async def read_specific_documentation(self, page_id: str) -> Dict[str, Any]:
        """Retrieve detailed content of a specific documentation page/article."""
        await self.ensure_authenticated()
        
        try:
            response = self.session.get(f"{self.base_url}/tutorial-pages/{page_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get documentation page: {str(e)}", "ERROR")
            raise

    # Badge status function removed as requested

# Initialize MCP server
mcp = FastMCP('brain_mcp_server')

# Initialize API client
brain_client = BrainApiClient()

# Configuration management
CONFIG_FILE = "user_config.json"

def _resolve_config_path(for_write: bool = False) -> str:
    """
    Resolve the config file path with this priority:
    1) BRAIN_CONFIG_PATH (file or directory)
    2) Directory of running script when available, else current working directory
    3) Current working directory

    When for_write=True, returns the preferred path even if it doesn't exist yet.
    """
    # 1) Explicit override via env var
    env_path = os.environ.get("BRAIN_CONFIG_PATH")
    if env_path:
        p = Path(env_path).expanduser()
        target = p / CONFIG_FILE if p.is_dir() else p
        # For read, only if it exists; for write, allow regardless
        if for_write or target.exists():
            return str(target.resolve())

    # 2) Script/module directory when available, else CWD (works in notebooks)
    base_dir = Path.cwd()
    try:
        # __file__ is not defined in notebooks; this will fail there and keep CWD
        script_dir = Path(__file__).resolve().parent  # type: ignore[name-defined]
        base_dir = script_dir
    except Exception:
        # Fall back to current working directory for notebooks/REPL
        pass

    module_path = base_dir / CONFIG_FILE
    if not for_write and module_path.exists():
        return str(module_path.resolve())

    # 3) Fallback to CWD for backward compatibility
    cwd_path = Path.cwd() / CONFIG_FILE
    if not for_write and cwd_path.exists():
        return str(cwd_path.resolve())

    # For writes (or when nothing exists), prefer the module/base directory
    return str(module_path.resolve())

def load_config() -> Dict[str, Any]:
    """Load configuration from file with robust path resolution.

    Looks for the config in this order: BRAIN_CONFIG_PATH -> module directory -> CWD.
    Returns an empty dict when not found or on error.
    """
    path = _resolve_config_path(for_write=False)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config from '{path}': {e}")
    return {}


def save_config(config: Dict[str, Any]):
    """Save configuration to file using the resolved config path.

    Uses BRAIN_CONFIG_PATH if set; otherwise writes next to this module.
    Ensures the target directory exists.
    """
    try:
        path = _resolve_config_path(for_write=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

# MCP Tools

@mcp.tool()
async def authenticate(email: Optional[str] = "", password: Optional[str] = "") -> Dict[str, Any]:
    """
    🔐 Authenticate with WorldQuant BRAIN platform.
    
    This is the first step in any BRAIN workflow. You must authenticate before using any other tools.
    
    Args:
        email: Your BRAIN platform email address (optional if in config or .brain_credentials)
        password: Your BRAIN platform password (optional if in config or .brain_credentials)
    
    Returns:
        Authentication result with user info and permissions
    """
    try:
        config = load_config()
        if 'credentials' in config:
            if not email:
                email = config['credentials'].get('email', '')
            if not password:
                password = config['credentials'].get('password', '')
        
        if not email or not password:
            return {"error": "Email and password required. Either provide them as arguments, configure them in user_config.json, or create a .brain_credentials file in your home directory with format: [\"email\", \"password\"]"}
        
        result = await brain_client.authenticate(email, password)
        
        # Save credentials to config for future use
        config = load_config()
        if 'credentials' not in config:
            config['credentials'] = {}
        config['credentials']['email'] = email
        config['credentials']['password'] = password
        save_config(config)
        
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def value_factor_trendScore(start_date: str, end_date: str) -> Dict[str, Any]:
    """Compute and return the diversity score for REGULAR alphas in a submission-date window.
    This function calculate the diversity of the users' submission, by checking the diversity, we can have a good understanding on the valuefactor's trend.
    This MCP tool wraps BrainApiClient.value_factor_trendScore and always uses submission dates (OS).

    Inputs:
        - start_date: ISO UTC start datetime (e.g. '2025-08-14T00:00:00Z')
        - end_date: ISO UTC end datetime (e.g. '2025-08-18T23:59:59Z')
        - p_max: optional integer total number of pyramid categories for normalization

    Returns: compact JSON with diversity_score, N, A, P, P_max, S_A, S_P, S_H, per_pyramid_counts
    """
    try:
        return await brain_client.value_factor_trendScore(start_date=start_date, end_date=end_date)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def manage_config(action: str = "get", settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    🔧 Manage configuration settings - get or update configuration.
    
    Args:
        action: Action to perform ("get" to retrieve config, "set" to update config)
        settings: Configuration settings to update (required when action="set")
    
    Returns:
        Current or updated configuration including authentication status
    """
    if action == "get":
        config = load_config()
        auth_status = await brain_client.get_authentication_status()
        
        return {
            "config": config,
            "auth_status": auth_status,
            "is_authenticated": await brain_client.is_authenticated()
        }
    
    elif action == "set":
        if settings is None:
            return {"error": "Settings parameter is required when action='set'"}
        
        config = load_config()
        config.update(settings)
        save_config(config)
        return config
    
    else:
        return {"error": f"Invalid action '{action}'. Use 'get' or 'set'."}

@mcp.tool()
async def create_simulation(
    type: str = "REGULAR",
    instrument_type: str = "EQUITY",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: float = 0.0,
    neutralization: str = "NONE",
    truncation: float = 0.0,
    test_period: str = "P0Y0M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    visualization: bool = True,
    regular: Optional[str] = None,
    combo: Optional[str] = None,
    selection: Optional[str] = None,
    pasteurization: str = "ON",
    max_trade: str = "OFF",
    selection_handling: str = "POSITIVE",
    selection_limit: int = 1000,
    component_activation: str = "IS"
) -> Dict[str, Any]:
    """
    🚀 Create a new simulation on BRAIN platform.
    
    This tool creates and starts a simulation with your alpha code. Use this after you have your alpha formula ready.
    
    Args:
        type: Simulation type ("REGULAR" or "SUPER")
        instrument_type: Type of instruments (e.g., "EQUITY")
        region: Market region (e.g., "USA")
        universe: Universe of stocks (e.g., "TOP3000")
        delay: Data delay (0 or 1)
        decay: Decay value for the simulation
        neutralization: Neutralization method
        truncation: Truncation value
        test_period: Test period (e.g., "P0Y0M" for 1 year 6 months)
        unit_handling: Unit handling method
        nan_handling: NaN handling method
        language: Expression language (e.g., "FASTEXPR")
        visualization: Enable visualization
        regular: Regular simulation code (for REGULAR type)
        combo: Combo code (for SUPER type)
        selection: Selection code (for SUPER type)
    
    Returns:
        Simulation creation result with ID and location
    """
    try:
        settings = SimulationSettings(
            instrumentType=instrument_type,
            region=region,
            universe=universe,
            delay=delay,
            decay=decay,
            neutralization=neutralization,
            truncation=truncation,
            testPeriod=test_period,
            unitHandling=unit_handling,
            nanHandling=nan_handling,
            language=language,
            visualization=visualization
        )
        
        simulation_data = SimulationData(
            type=type,
            settings=settings,
            regular=regular,
            combo=combo,
            selection=selection
        )
        
        result = await brain_client.create_simulation(simulation_data)
        return result
    except Exception as e:
        return {"error": str(e), "note":", you need to call three mcp tools get_operators, get_platform_setting_options and get_datafields to check whether you correctly use the operators, setting the simulation settings, and existing data fields."}

# get_simulation_status MCP tool removed as requested
# wait_for_simulation MCP tool removed as requested

@mcp.tool()
async def get_alpha_details(alpha_id: str) -> Dict[str, Any]:
    """
    📋 Get detailed information about an alpha.
    
    Args:
        alpha_id: The ID of the alpha to retrieve
    
    Returns:
        Detailed alpha information
    """
    try:
        return await brain_client.get_alpha_details(alpha_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_datasets(
    instrument_type: str = "EQUITY",
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    theme: str = "ALL",
    search: Optional[str] = None
) -> Dict[str, Any]:
    """
    📚 Get available datasets for research.
    
    Use this to discover what data is available for your alpha research.
    
    Args:
        instrument_type: Type of instruments (e.g., "EQUITY")
        region: Market region (e.g., "USA")
        delay: Data delay (0 or 1)
        universe: Universe of stocks (e.g., "TOP3000")
        theme: Theme filter
    
    Returns:
        Available datasets
    """
    try:
        return await brain_client.get_datasets(instrument_type, region, delay, universe, theme,search)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_datafields(
    instrument_type: str = "EQUITY",
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    theme: str = "false",
    dataset_id: Optional[str] = None,
    data_type: str = "",
    search: Optional[str] = None
) -> Dict[str, Any]:
    """
    🔍 Get available data fields for alpha construction.
    
    Use this to find specific data fields you can use in your alpha formulas.
    
    Args:
        instrument_type: Type of instruments (e.g., "EQUITY")
        region: Market region (e.g., "USA")
        delay: Data delay (0 or 1)
        universe: Universe of stocks (e.g., "TOP3000")
        theme: Theme filter
        dataset_id: Specific dataset ID to filter by
        data_type: Type of data (e.g., "MATRIX")
        search: Search term to filter fields
    
    Returns:
        Available data fields
    """
    try:
        return await brain_client.get_datafields(
            instrument_type, region, delay, universe, theme, 
            dataset_id, data_type, search
        )
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_alpha_pnl(alpha_id: str) -> Dict[str, Any]:
    """
    📈 Get PnL (Profit and Loss) data for an alpha.
    
    Args:
        alpha_id: The ID of the alpha
    
    Returns:
        PnL data for the alpha
    """
    try:
        return await brain_client.get_alpha_pnl(alpha_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_user_alphas(
    stage: str = "IS",
    limit: int = 30,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    submission_start_date: Optional[str] = None,
    submission_end_date: Optional[str] = None,
    order: Optional[str] = None,
    hidden: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    👤 Get user's alphas with advanced filtering, pagination, and sorting.

    This tool retrieves a list of your alphas, allowing for detailed filtering based on stage,
    creation date, submission date, and visibility. It also supports pagination and custom sorting.

    Args:
        stage (str): The stage of the alphas to retrieve.
            - "IS": In-Sample (alphas that have not been submitted).
            - "OS": Out-of-Sample (alphas that have been submitted).
            Defaults to "IS".
        limit (int): The maximum number of alphas to return in a single request.
            For example, `limit=50` will return at most 50 alphas. Defaults to 30.
        offset (int): The number of alphas to skip from the beginning of the list.
            Used for pagination. For example, `limit=50, offset=50` will retrieve alphas 51-100.
            Defaults to 0.
        start_date (Optional[str]): The earliest creation date for the alphas to be included.
            Filters for alphas created on or after this date.
            Example format: "2023-01-01T00:00:00Z".
        end_date (Optional[str]): The latest creation date for the alphas to be included.
            Filters for alphas created before this date.
            Example format: "2023-12-31T23:59:59Z".
        submission_start_date (Optional[str]): The earliest submission date for the alphas.
            Only applies to "OS" alphas. Filters for alphas submitted on or after this date.
            Example format: "2024-01-01T00:00:00Z".
        submission_end_date (Optional[str]): The latest submission date for the alphas.
            Only applies to "OS" alphas. Filters for alphas submitted before this date.
            Example format: "2024-06-30T23:59:59Z".
        order (Optional[str]): The sorting order for the returned alphas.
            Prefix with a hyphen (-) for descending order.
            Examples: "name" (sort by name ascending), "-dateSubmitted" (sort by submission date descending).
        hidden (Optional[bool]): Filter alphas based on their visibility.
            - `True`: Only return hidden alphas.
            - `False`: Only return non-hidden alphas.
            If not provided, both hidden and non-hidden alphas are returned.

    Returns:
        Dict[str, Any]: A dictionary containing a list of alpha details under the 'results' key,
        along with pagination information. If an error occurs, it returns a dictionary with an 'error' key.
    """
    try:
        return await brain_client.get_user_alphas(
            stage=stage,
            limit=limit,
            offset=offset,
            start_date=start_date,
            end_date=end_date,
            submission_start_date=submission_start_date,
            submission_end_date=submission_end_date,
            order=order,
            hidden=hidden,
        )
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def submit_alpha(alpha_id: str) -> Dict[str, Any]:
    """
    📤 Submit an alpha for production.
    
    Use this when your alpha is ready for production deployment.
    
    Args:
        alpha_id: The ID of the alpha to submit
    
    Returns:
        Submission result
    """
    try:
        success = await brain_client.submit_alpha(alpha_id)
        return {"submit_result": success, "alpha_id": alpha_id}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_events() -> Dict[str, Any]:
    """
    🏆 Get available events and competitions.
    
    Returns:
        Available events and competitions
    """
    try:
        return await brain_client.get_events()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_leaderboard(user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    🏅 Get leaderboard data.
    
    Args:
        user_id: Optional user ID to filter results
    
    Returns:
        Leaderboard data
    """
    try:
        return await brain_client.get_leaderboard(user_id)
    except Exception as e:
        return {"error": str(e)}

# batch_process_alphas MCP tool removed as requested

@mcp.tool()
async def save_simulation_data(simulation_id: str, filename: str) -> Dict[str, Any]:
    """
    💾 Save simulation data to a file.
    
    Args:
        simulation_id: The simulation ID
        filename: Filename to save the data
    
    Returns:
        Save operation result
    """
    try:
        # Get simulation data
        simulation_data = await brain_client.get_simulation_status(simulation_id)
        
        # Save to file
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(simulation_data, f, indent=2)
        
        return {"success": True, "filename": filename, "simulation_id": simulation_id}
    except Exception as e:
        return {"error": str(e)}



@mcp.tool()
async def get_operators() -> Dict[str, Any]:
    """
    🔧 Get available operators for alpha creation.
    
    Returns:
        Dictionary containing operators list and count
    """
    try:
        return await brain_client.get_operators()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def run_selection(
    selection: str,
    instrument_type: str = "EQUITY",
    region: str = "USA",
    delay: int = 1,
    selection_limit: int = 1000,
    selection_handling: str = "POSITIVE"
) -> Dict[str, Any]:
    """
    🎯 Run a selection query to filter instruments.
    
    Args:
        selection: Selection criteria
        instrument_type: Type of instruments
        region: Geographic region
        delay: Delay setting
        selection_limit: Maximum number of results
        selection_handling: How to handle selection results
    
    Returns:
        Selection results
    """
    try:
        return await brain_client.run_selection(
            selection, instrument_type, region, delay, selection_limit, selection_handling
        )
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_user_profile(user_id: str = "self") -> Dict[str, Any]:
    """
    👤 Get user profile information.
    
    Args:
        user_id: User ID (default: "self" for current user)
    
    Returns:
        User profile data
    """
    try:
        return await brain_client.get_user_profile(user_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_documentations() -> Dict[str, Any]:
    """
    📚 Get available documentations and learning materials.
    
    Returns:
        List of documentations
    """
    try:
        return await brain_client.get_documentations()
    except Exception as e:
        return {"error": str(e)}

# get_messages_summary MCP tool removed as requested

@mcp.tool()
async def get_messages(limit: Optional[int] = 0, offset: int = 0) -> Dict[str, Any]:
    """
    Get messages for the current user with optional pagination.
    
    Args:
        limit: Maximum number of messages to return (e.g., 10 for top 10 messages)
               Can be None (no limit), an integer, or a string that can be converted to int
        offset: Number of messages to skip (for pagination)
               Can be an integer or a string that can be converted to int
    
    Returns:
        Messages for the current user, optionally limited by count
    """
    # Wrap the entire function in a try-catch to handle any encoding issues
    try:
        # Enhanced parameter validation and conversion
        validated_limit = None
        validated_offset = 0
        
        # Validate and convert limit parameter
        if limit is not None:
            if isinstance(limit, str):
                if limit.strip() == "":
                    # Empty string means no limit
                    validated_limit = 0
                else:
                    try:
                        validated_limit = int(limit)
                        if validated_limit < 0:
                            return {"error": f"Limit must be non-negative, got: {limit}"}
                    except ValueError:
                        return {"error": f"Invalid limit value '{limit}'. Must be a number or empty string."}
            elif isinstance(limit, (int, float)):
                validated_limit = int(limit)
                if validated_limit < 0:
                    return {"error": f"Limit must be non-negative, got: {limit}"}
            else:
                return {"error": f"Invalid limit type {type(limit).__name__}. Expected int, float, str, or None."}
        
        # Validate and convert offset parameter
        if isinstance(offset, str):
            try:
                validated_offset = int(offset)
            except ValueError:
                return {"error": f"Invalid offset value '{offset}'. Must be a number."}
        elif isinstance(offset, (int, float)):
            validated_offset = int(offset)
        else:
            return {"error": f"Invalid offset type {type(offset).__name__}. Expected int, float, or str."}
        
        if validated_offset < 0:
            return {"error": f"Offset must be non-negative, got: {offset}"}
        
        # Log the validated parameters for debugging (without emojis to avoid encoding issues)
        try:
            print(f"get_messages called with validated parameters: limit={validated_limit}, offset={validated_offset}")
        except Exception:
            print(f"get_messages called with parameters: limit={validated_limit}, offset={validated_offset}")
        
        # Call the brain client with validated parameters
        result = await brain_client.get_messages(validated_limit, validated_offset)
        
        # Add validation info to the result
        if isinstance(result, dict) and "error" not in result:
            result["_validation"] = {
                "original_limit": limit,
                "original_offset": offset,
                "validated_limit": validated_limit,
                "validated_offset": validated_offset,
                "parameter_types": {
                    "limit": str(type(limit)),
                    "offset": str(type(offset))
                }
            }
        
        return result
        
    except UnicodeEncodeError as ue:
        # Handle encoding errors specifically
        error_msg = f"get_messages failed due to encoding error: {str(ue)}"
        try:
            print(f"ENCODING ERROR: {error_msg}")
        except Exception:
            print(f"get_messages encoding error: {str(ue)}")
        return {
            "error": error_msg,
            "error_type": "UnicodeEncodeError",
            "original_params": {
                "limit": limit,
                "offset": offset,
                "limit_type": str(type(limit)),
                "offset_type": str(type(offset))
            }
        }
    except Exception as e:
        error_msg = f"get_messages failed: {str(e)}"
        try:
            print(f"ERROR: {error_msg}")
        except Exception:
            print(f"get_messages failed: {str(e)}")
        return {
            "error": error_msg,
            "error_type": type(e).__name__,
            "original_params": {
                "limit": limit,
                "offset": offset,
                "limit_type": str(type(limit)),
                "offset_type": str(type(offset))
            }
        }

@mcp.tool()
async def get_glossary_terms(email: str = "", password: str = "", headless: bool = False) -> Dict[str, Any]:
    """
    📚 Get glossary terms from WorldQuant BRAIN forum.
    
    Note: This requires Selenium and is implemented in forum_functions.py
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
        headless: Run browser in headless mode (default: False)
    
    Returns:
        Glossary terms with definitions
    """
    try:
        # Load config to get credentials if not provided
        config = load_config()
        
        # Use provided credentials or fall back to config
        if not email and 'credentials' in config:
            email = config['credentials'].get('email', '')
        if not password and 'credentials' in config:
            password = config['credentials'].get('password', '')
        
        if not email or not password:
            return {"error": "Email and password required. Either provide them as arguments or configure them in user_config.json"}
        
        return await brain_client.get_glossary_terms(email, password, headless)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def search_forum_posts(search_query: str, email: str = "", password: str = "", 
                           max_results: int = 50, headless: bool = True) -> Dict[str, Any]:
    """
    🔍 Search forum posts on WorldQuant BRAIN support site.
    
    Note: This requires Selenium and is implemented in forum_functions.py
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
        search_query: Search term or phrase
        max_results: Maximum number of results to return (default: 50)
        headless: Run browser in headless mode (default: True)
    
    Returns:
        Search results with analysis
    """
    try:
        # Load config to get credentials if not provided
        config = load_config()
        
        # Use provided credentials or fall back to config
        if not email and 'credentials' in config:
            email = config['credentials'].get('email', '')
        if not password and 'credentials' in config:
            password = config['credentials'].get('password', '')
        
        if not email or not password:
            return {"error": "Email and password required. Either provide them as arguments or configure them in user_config.json"}
        
        return await brain_client.search_forum_posts(email, password, search_query, max_results, headless)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def read_forum_post(article_id: str, email: str = "", password: str = "", 
                        headless: bool = False) -> Dict[str, Any]:
    """
    📄 Get a specific forum post by article ID.
    
    Note: This requires Selenium and is implemented in forum_functions.py
    
    Args:
        article_id: The article ID to retrieve (e.g., "32984819083415-新人求模板")
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
        headless: Run browser in headless mode (default: False)
    
    Returns:
        Forum post content with comments
    """
    try:
        # Load config to get credentials if not provided
        config = load_config()
        
        # Use provided credentials or fall back to config
        if not email and 'credentials' in config:
            email = config['credentials'].get('email', '')
        if not password and 'credentials' in config:
            password = config['credentials'].get('password', '')
        
        if not email or not password:
            return {"error": "Email and password required. Either provide them as arguments or configure them in user_config.json"}
        
        # Import and use forum functions directly
        from forum_functions import forum_client
        return await forum_client.read_full_forum_post(email, password, article_id, headless, include_comments=True)
    except ImportError:
        return {"error": "Forum functions require selenium. Use forum_functions.py directly."}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_alpha_yearly_stats(alpha_id: str) -> Dict[str, Any]:
    """Get yearly statistics for an alpha."""
    try:
        return await brain_client.get_alpha_yearly_stats(alpha_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def check_correlation(alpha_id: str, correlation_type: str = "both", threshold: float = 0.7) -> Dict[str, Any]:
    """Check alpha correlation against production alphas, self alphas, or both."""
    try:
        return await brain_client.check_correlation(alpha_id, correlation_type, threshold)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_submission_check(alpha_id: str) -> Dict[str, Any]:
    """Comprehensive pre-submission check."""
    try:
        return await brain_client.get_submission_check(alpha_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def set_alpha_properties(alpha_id: str, name: Optional[str] = None, 
                             color: Optional[str] = None, tags: List[str] = None,
                             selection_desc: str = "None", combo_desc: str = "None") -> Dict[str, Any]:
    """Update alpha properties (name, color, tags, descriptions)."""
    try:
        return await brain_client.set_alpha_properties(alpha_id, name, color, tags, selection_desc, combo_desc)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_record_sets(alpha_id: str) -> Dict[str, Any]:
    """List available record sets for an alpha."""
    try:
        return await brain_client.get_record_sets(alpha_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_record_set_data(alpha_id: str, record_set_name: str) -> Dict[str, Any]:
    """Get data from a specific record set."""
    try:
        return await brain_client.get_record_set_data(alpha_id, record_set_name)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_user_activities(user_id: str, grouping: Optional[str] = None) -> Dict[str, Any]:
    """Get user activity diversity data."""
    try:
        return await brain_client.get_user_activities(user_id, grouping)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_pyramid_multipliers() -> Dict[str, Any]:
    """Get current pyramid multipliers showing BRAIN's encouragement levels."""
    try:
        return await brain_client.get_pyramid_multipliers()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_pyramid_alphas(start_date: Optional[str] = None,
                           end_date: Optional[str] = None) -> Dict[str, Any]:
    """Get user's current alpha distribution across pyramid categories."""
    try:
        return await brain_client.get_pyramid_alphas(start_date, end_date)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_user_competitions(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Get list of competitions that the user is participating in."""
    try:
        return await brain_client.get_user_competitions(user_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_competition_details(competition_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific competition."""
    try:
        return await brain_client.get_competition_details(competition_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_competition_agreement(competition_id: str) -> Dict[str, Any]:
    """Get the rules, terms, and agreement for a specific competition."""
    try:
        return await brain_client.get_competition_agreement(competition_id)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def get_platform_setting_options() -> Dict[str, Any]:
    """Discover valid simulation setting options (instrument types, regions, delays, universes, neutralization).

    Use this when a simulation request might contain an invalid/mismatched setting. If an AI or user supplies
    incorrect parameters (e.g., wrong region for an instrument type), call this tool to retrieve the authoritative
    option sets and correct the inputs before proceeding.

    Returns:
        A structured list of valid combinations and choice lists to validate or fix simulation settings.
    """
    try:
        return await brain_client.get_platform_setting_options()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def performance_comparison(alpha_id: str, team_id: Optional[str] = None, 
                               competition: Optional[str] = None) -> Dict[str, Any]:
    """Get performance comparison data for an alpha."""
    try:
        return await brain_client.performance_comparison(alpha_id, team_id, competition)
    except Exception as e:
        return {"error": str(e)}

# combine_test_results MCP tool removed as requested

@mcp.tool()
async def expand_nested_data(data: List[Dict[str, Any]], preserve_original: bool = True) -> List[Dict[str, Any]]:
    """Flatten complex nested data structures into tabular format."""
    try:
        return await brain_client.expand_nested_data(data, preserve_original)
    except Exception as e:
        return {"error": str(e)}

# generate_alpha_links MCP tool removed as requested

@mcp.tool()
async def read_specific_documentation(page_id: str) -> Dict[str, Any]:
    """Retrieve detailed content of a specific documentation page/article."""
    try:
        return await brain_client.read_specific_documentation(page_id)
    except Exception as e:
        return {"error": str(e)}

# Badge status MCP tool removed as requested

@mcp.tool()
async def create_multiSim(
    alpha_expressions: List[str],
    instrument_type: str = "EQUITY",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: float = 0.0,
    neutralization: str = "NONE",
    truncation: float = 0.0,
    test_period: str = "P0Y0M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    visualization: bool = True,
    pasteurization: str = "ON",
    max_trade: str = "OFF"
) -> Dict[str, Any]:
    """
    🚀 Create multiple regular alpha simulations on BRAIN platform in a single request.
    
    This tool creates a multisimulation with multiple regular alpha expressions,
    waits for all simulations to complete, and returns detailed results for each alpha.
    
    ⏰ NOTE: Multisimulations can take 8+ minutes to complete. This tool will wait
    for the entire process and return comprehensive results.
    Call get_platform_setting_options to get the valid options for the simulation.
    Args:
        alpha_expressions: List of alpha expressions (2-8 expressions required)
        instrument_type: Type of instruments (default: "EQUITY")
        region: Market region (default: "USA")
        universe: Universe of stocks (default: "TOP3000")
        delay: Data delay (default: 1)
        decay: Decay value (default: 0.0)
        neutralization: Neutralization method (default: "NONE")
        truncation: Truncation value (default: 0.0)
        test_period: Test period (default: "P0Y0M")
        unit_handling: Unit handling method (default: "VERIFY")
        nan_handling: NaN handling method (default: "OFF")
        language: Expression language (default: "FASTEXPR")
        visualization: Enable visualization (default: True)
        pasteurization: Pasteurization setting (default: "ON")
        max_trade: Max trade setting (default: "OFF")
    
    Returns:
        Dictionary containing multisimulation results and individual alpha details
    """
    try:
        # Validate input
        if len(alpha_expressions) < 2:
            return {"error": "At least 2 alpha expressions are required"}
        if len(alpha_expressions) > 8:
            return {"error": "Maximum 8 alpha expressions allowed per request"}
        
        # Create multisimulation data
        multisimulation_data = []
        for alpha_expr in alpha_expressions:
            simulation_item = {
                'type': 'REGULAR',
                'settings': {
                    'instrumentType': instrument_type,
                    'region': region,
                    'universe': universe,
                    'delay': delay,
                    'decay': decay,
                    'neutralization': neutralization,
                    'truncation': truncation,
                    'pasteurization': pasteurization,
                    'unitHandling': unit_handling,
                    'nanHandling': nan_handling,
                    'language': language,
                    'visualization': visualization,
                    'testPeriod': test_period,
                    'maxTrade': max_trade
                },
                'regular': alpha_expr
            }
            multisimulation_data.append(simulation_item)
        
        # Send multisimulation request
        response = brain_client.session.post(f"{brain_client.base_url}/simulations", json=multisimulation_data)
        
        if response.status_code != 201:
            return {"error": f"Failed to create multisimulation. Status: {response.status_code},, you need to call three mcp tools get_operators, get_platform_setting_options and get_datafields to check whether you correctly use the operators, setting the simulation settings, and existing data fields."}
        
        # Get multisimulation location
        location = response.headers.get('Location', '')
        if not location:
            return {"error": "No location header in multisimulation response"}
        
        # Wait for children to appear and get results
        return await _wait_for_multisimulation_completion(location, len(alpha_expressions))
        
    except Exception as e:
        return {"error": f"Error creating multisimulation: {str(e)}, , you need to call three mcp tools get_operators, get_platform_setting_options and get_datafields to check whether you correctly use the operators, setting the simulation settings, and existing data fields."}

async def _wait_for_multisimulation_completion(location: str, expected_children: int) -> Dict[str, Any]:
    """Wait for multisimulation to complete and return results"""
    try:
        # Simple progress indicator for users
        print(f"Waiting for multisimulation to complete... (this may take several minutes)")
        print(f"Expected {expected_children} alpha simulations")
        print()
        # Wait for children to appear - much more tolerant for 8+ minute multisimulations
        children = []
        max_wait_attempts = 200  # Increased significantly for 8+ minute multisimulations
        wait_attempt = 0
        
        while wait_attempt < max_wait_attempts and len(children) == 0:
            wait_attempt += 1
            
            try:
                multisim_response = brain_client.session.get(location)
                if multisim_response.status_code == 200:
                    multisim_data = multisim_response.json()
                    children = multisim_data.get('children', [])
                    
                    if children:
                        break
                    else:
                        # Wait before next attempt - use longer intervals for multisimulations
                        retry_after = multisim_response.headers.get("Retry-After", 5)
                        wait_time = float(retry_after)
                        await asyncio.sleep(wait_time)
                else:
                    await asyncio.sleep(5)
            except Exception as e:
                await asyncio.sleep(5)
        
        if not children:
            return {"error": f"Children did not appear within {max_wait_attempts} attempts (multisimulation may still be processing)"}
        
        # Process each child to get alpha results
        alpha_results = []
        for i, child_id in enumerate(children):
            try:
                # The children are full URLs, not just IDs
                child_url = child_id if child_id.startswith('http') else f"{brain_client.base_url}/simulations/{child_id}"
                
                # Wait for this alpha to complete - more tolerant timing
                finished = False
                max_alpha_attempts = 100  # Increased for longer alpha processing
                alpha_attempt = 0
                
                while not finished and alpha_attempt < max_alpha_attempts:
                    alpha_attempt += 1
                    
                    try:
                        alpha_progress = brain_client.session.get(child_url)
                        if alpha_progress.status_code == 200:
                            alpha_data = alpha_progress.json()
                            retry_after = alpha_progress.headers.get("Retry-After", 0)
                            
                            if retry_after == 0:
                                finished = True
                                break
                            else:
                                wait_time = float(retry_after)
                                await asyncio.sleep(wait_time)
                        else:
                            await asyncio.sleep(5)
                    except Exception as e:
                        await asyncio.sleep(5)
                
                if finished:
                    # Get alpha details from the completed simulation
                    alpha_id = alpha_data.get("alpha")
                    if alpha_id:
                        # Now get the actual alpha details from the alpha endpoint
                        alpha_details = brain_client.session.get(f"{brain_client.base_url}/alphas/{alpha_id}")
                        if alpha_details.status_code == 200:
                            alpha_detail_data = alpha_details.json()
                            alpha_results.append({
                                'alpha_id': alpha_id,
                                'location': child_url,
                                'details': alpha_detail_data
                            })
                        else:
                            alpha_results.append({
                                'alpha_id': alpha_id,
                                'location': child_url,
                                'error': f'Failed to get alpha details: {alpha_details.status_code}'
                            })
                    else:
                        alpha_results.append({
                            'location': child_url,
                            'error': 'No alpha ID found in completed simulation'
                        })
                else:
                    alpha_results.append({
                        'location': child_url,
                        'error': f'Alpha simulation did not complete within {max_alpha_attempts} attempts'
                    })
                    
            except Exception as e:
                alpha_results.append({
                    'location': f"child_{i+1}",
                    'error': str(e)
                })
        
        # Return comprehensive results
        print(f"Multisimulation completed! Retrieved {len(alpha_results)} alpha results")
        return {
            'success': True,
            'message': f'Successfully created {expected_children} regular alpha simulations',
            'total_requested': expected_children,
            'total_created': len(alpha_results),
            'multisimulation_id': location.split('/')[-1],
            'multisimulation_location': location,
            'alpha_results': alpha_results,
            'note': "if you got a negative alpha sharpe, you can just add a minus sign in front of the last line of the Alpha to flip then think the next step."
        }
        
    except Exception as e:
        return {"error": f"Error waiting for multisimulation completion: {str(e)}, you need to call three mcp tools get_operators, get_platform_setting_options and get_datafields to check whether you correctly use the operators, setting the simulation settings, and existing data fields."}

@mcp.tool()
async def get_daily_and_quarterly_payment(email: str = "", password: str = "") -> Dict[str, Any]:
    """
    Get daily and quarterly payment information from WorldQuant BRAIN platform.
    
    This function retrieves both base payments (daily alpha performance payments) and 
    other payments (competition rewards, quarterly payments, referrals, etc.).
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        Dictionary containing base payment and other payment data with summaries and detailed records
    """
    try:
        # Authenticate if credentials provided
        if email and password:
            auth_result = await brain_client.authenticate(email, password)
            if auth_result.get('status') != 'authenticated':
                return {"error": f"Authentication failed: {auth_result.get('message', 'Unknown error')}"}
        else:
            # Try to use existing session or config
            config = await manage_config("get")
            if not config.get('is_authenticated'):
                return {"error": "Not authenticated. Please provide email and password or authenticate first."}
        

        # Set required Accept header for API v3.0
        header = {"Accept": "application/json;version=3.0"}

        # Get base payment data
        base_payment_response = brain_client.session.get(
            'https://api.worldquantbrain.com/users/self/activities/base-payment', headers=header
        )

        if base_payment_response.status_code != 200:
            return {"error": f"Failed to get base payment data: {base_payment_response.status_code}"}

        base_payment_data = base_payment_response.json()

        # Get other payment data
        other_payment_response = brain_client.session.get(
            'https://api.worldquantbrain.com/users/self/activities/other-payment', headers=header
        )

        if other_payment_response.status_code != 200:
            return {"error": f"Failed to get other payment data: {other_payment_response.status_code}"}

        other_payment_data = other_payment_response.json()
        
        # Return comprehensive payment information
        return {
            "success": True,
            "base_payment": {
                "summary": {
                    "yesterday": base_payment_data.get("yesterday"),
                    "current_quarter": base_payment_data.get("current"),
                    "previous_quarter": base_payment_data.get("previous"),
                    "year_to_date": base_payment_data.get("ytd"),
                    "total_all_time": base_payment_data.get("total"),
                    "currency": base_payment_data.get("currency")
                },
                "daily_records": base_payment_data.get("records", {}).get("records", []),
                "schema": base_payment_data.get("records", {}).get("schema")
            },
            "other_payment": {
                "total_all_time": other_payment_data.get("total"),
                "special_payments": other_payment_data.get("records", {}).get("records", []),
                "schema": other_payment_data.get("records", {}).get("schema"),
                "currency": other_payment_data.get("currency")
            },
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        return {"error": f"Error retrieving payment information: {str(e)}"}



# New MCP tool: get_SimError_detail
from typing import Sequence
@mcp.tool()
async def get_SimError_detail(locations: Sequence[str]) -> dict:
    """
    Fetch and parse error/status from multiple simulation locations (URLs).
    Args:
        locations: List of simulation result URLs (e.g., /simulations/{id})
    Returns:
        List of dicts with location, error message, and raw response
    """
    results = []
    for loc in locations:
        try:
            resp = brain_client.session.get(loc)
            if resp.status_code != 200:
                results.append({
                    "location": loc,
                    "error": f"HTTP {resp.status_code}",
                    "raw": resp.text
                })
                continue
            data = resp.json() if resp.text else {}
            # Try to extract error message or status
            error_msg = data.get("error") or data.get("message")
            # If alpha ID is missing, include that info
            if not data.get("alpha"):
                error_msg = error_msg or "Simulation did not get through, if you are running a multisimulation, check the other children location in your request"
            results.append({
                "location": loc,
                "error": error_msg,
                "raw": data
            })
        except Exception as e:
            results.append({
                "location": loc,
                "error": str(e),
                "raw": None
            })
    return {"results": results}

if __name__ == "__main__":
    try:
        print("WorldQuant BRAIN MCP Server Starting...", file=sys.stderr)
        mcp.run()
    except Exception as e:
        print(f"Failed to start MCP server: {e}", file=sys.stderr)
        sys.exit(1)
    