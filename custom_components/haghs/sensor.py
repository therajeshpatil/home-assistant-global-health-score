"""HAGHS - Home Assistant Global Health Score Sensor (v2.0.2 Logic)."""
import logging
import math
from datetime import timedelta

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import (
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

# Defaults
DEFAULT_NAME = "System: HA - Global Health Score"
SCAN_INTERVAL = timedelta(minutes=1)

# Configuration Keys
CONF_CPU_ID = "cpu_sensor"
CONF_RAM_ID = "ram_sensor"
CONF_DISK_ID = "disk_sensor"
CONF_DB_ID = "db_sensor"
CONF_LOG_ID = "log_sensor"
CONF_CORE_UPDATE_ID = "core_update_entity"
CONF_IGNORE_LABEL = "ignore_label"

# Domains to check for Zombies
ZOMBIE_DOMAINS = [
    "sensor", "binary_sensor", "switch", "light", "fan",
    "climate", "media_player", "vacuum", "camera"
]

# Validation Schema
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_CPU_ID): cv.entity_id,
    vol.Required(CONF_RAM_ID): cv.entity_id,
    vol.Required(CONF_DISK_ID): cv.entity_id,
    vol.Required(CONF_DB_ID): cv.entity_id,
    vol.Optional(CONF_LOG_ID): cv.entity_id, # Optional/Graceful
    vol.Optional(CONF_CORE_UPDATE_ID, default="update.home_assistant_core_update"): cv.entity_id,
    vol.Optional(CONF_IGNORE_LABEL, default="haghs_ignore"): cv.string,
})

def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the HAGHS sensor."""
    add_entities([HaghsSensor(hass, config)])


class HaghsSensor(SensorEntity):
    """Representation of the HAGHS Sensor."""

    def __init__(self, hass, config):
        """Initialize the sensor."""
        self.hass = hass
        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = "system_ha_global_health_score"
        self._attr_icon = "mdi:shield-check"
        self._attr_native_unit_of_measurement = "%"
        
        # Load Config
        self.cpu_id = config[CONF_CPU_ID]
        self.ram_id = config[CONF_RAM_ID]
        self.disk_id = config[CONF_DISK_ID]
        self.db_id = config[CONF_DB_ID]
        self.log_id = config.get(CONF_LOG_ID)
        self.core_update_id = config[CONF_CORE_UPDATE_ID]
        self.ignore_label = config[CONF_IGNORE_LABEL]

    def update(self) -> None:
        """Fetch new state data and calculate score."""
        
        # --- 1. PILLAR: HARDWARE (40%) ---
        
        # CPU (Heavyweight Tiers)
        cpu = self._get_float(self.cpu_id)
        p_cpu = 0
        if cpu <= 10: p_cpu = 0
        elif cpu <= 15: p_cpu = 10
        elif cpu <= 25: p_cpu = 25
        elif cpu <= 50: p_cpu = 50
        else: p_cpu = 80
        score_cpu = 100 - p_cpu

        # RAM (Linear penalty > 70%)
        ram = self._get_float(self.ram_id)
        score_ram = 100
        if ram >= 70:
            score_ram = max(0, 100 - (ram - 70) * 3.33)

        # Disk (Linear penalty > 80%)
        disk = self._get_float(self.disk_id)
        score_disk = 100
        if disk >= 80:
            score_disk = max(0, 100 - (disk - 80) * 5)

        hardware_final = (score_cpu + score_ram + score_disk) / 3


        # --- 2. PILLAR: APPLICATION (60%) ---
        
        # A. ZOMBIES & LABELS
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        
        zombie_list = []
        
        # Iterate all states
        for state in self.hass.states.all():
            if state.domain not in ZOMBIE_DOMAINS:
                continue
            
            if state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
                entity_id = state.entity_id
                
                # Exclude Integration Health entities
                if "integration_health" in entity_id:
                    continue

                # Check Labels (Entity & Device)
                is_ignored = False
                
                # 1. Check Entity Label
                entity_entry = ent_reg.async_get(entity_id)
                if entity_entry and self.ignore_label in entity_entry.labels:
                    is_ignored = True
                
                # 2. Check Device Label
                if not is_ignored and entity_entry and entity_entry.device_id:
                    device_entry = dev_reg.async_get(entity_entry.device_id)
                    if device_entry and self.ignore_label in device_entry.labels:
                        is_ignored = True
                
                if not is_ignored:
                    zombie_list.append(entity_id)

        zombie_count = len(zombie_list)
        # Cap penalty at 20 (Logic: [0, count*2, 20])
        p_zombie = min(20, zombie_count * 2)

        # B. INTEGRATION HEALTH
        # Search for sensors containing 'integration_health' with state 'unhealthy'
        failed_integrations = 0
        for state in self.hass.states.all():
            if state.domain == "sensor" and "integration_health" in state.entity_id:
                if state.state == "unhealthy":
                    failed_integrations += 1
        
        p_integration = min(15, failed_integrations * 5)

        # C. MAINTENANCE (DB & LOGS)
        
        # DB Logic
        db_mb = self._get_float(self.db_id)
        p_db = 0
        if db_mb < 1000: p_db = 0
        elif db_mb < 2500: p_db = 10
        else: p_db = 30
        
        # Log Logic (Graceful degradation if missing)
        log_mb = 0.0
        p_log = 0
        if self.log_id:
            log_mb = self._get_float(self.log_id)
            if log_mb < 20: p_log = 0
            elif log_mb < 100: p_log = 10
            else: p_log = 25

        # D. UPDATES & BACKUPS
        
        # Backups Stale
        backup_state = self.hass.states.get("binary_sensor.backups_stale")
        p_backup = 30 if (backup_state and backup_state.state == "on") else 0
        
        # Updates Pending
        update_count = 0
        for state in self.hass.states.all():
            if state.domain == "update" and state.state == "on":
                update_count += 1
        
        # Core Version Lag
        p_core_lag = 0
        core_state = self.hass.states.get(self.core_update_id)
        if core_state:
            current = core_state.attributes.get("installed_version")
            latest = core_state.attributes.get("latest_version")
            
            if current and latest and "." in current and "." in latest:
                try:
                    cur_parts = [int(x) for x in current.split(".")[:2]]
                    lat_parts = [int(x) for x in latest.split(".")[:2]]
                    
                    cur_months = (cur_parts[0] * 12) + cur_parts[1]
                    lat_months = (lat_parts[0] * 12) + lat_parts[1]
                    
                    if (lat_months - cur_months) >= 2:
                        p_core_lag = 20
                except (ValueError, IndexError):
                    pass # Version parsing failed, ignore penalty

        p_updates = min(35, (update_count * 5) + p_core_lag)

        # --- FINAL CALCULATION ---
        app_final = 100 - p_zombie - p_integration - p_backup - p_updates - p_db - p_log
        app_final = max(0, app_final) # No negative scores

        global_score = math.floor((hardware_final * 0.4) + (app_final * 0.6))
        self._attr_native_value = int(global_score)

        # --- ATTRIBUTES & ADVISOR ---
        
        advice = []
        if p_cpu > 0: advice.append(f"⚡ Optimization: CPU load is impacting score ({cpu:.1f}%).")
        if disk >= 80: advice.append("⚠️ Disk Space: Drive full (>80%). Clean up!")
        if db_mb > 1000: advice.append(f"🗄️ Database: Huge DB ({db_mb/1000:.1f} GB). Check Recorder.")
        if log_mb > 25: advice.append(f"📜 Log File: Large Logs ({int(log_mb)} MB). Errors detected?")
        if p_backup > 0: advice.append("🚨 Security: Stale backup detected!")
        if update_count > 0: advice.append(f"📦 Maintenance: {update_count} update(s) pending.")
        if p_zombie > 0: advice.append(f"🧟 Hygiene: {zombie_count} Zombie Entities detected.")
        if p_core_lag > 0: advice.append("👴 Legacy: Core version is >2 months old.")

        advisor_text = "\n".join(advice) if advice else "✅ System optimized"

        self._attr_extra_state_attributes = {
            "hardware_score": int(hardware_final),
            "application_score": int(app_final),
            "zombie_count": zombie_count,
            "zombie_entities": ", ".join(zombie_list) if zombie_list else "None",
            "db_size_mb": round(db_mb, 1),
            "log_size_mb": round(log_mb, 1),
            "recommendations": advisor_text
        }

    def _get_float(self, entity_id):
        """Helper to safely get float state."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
            return 0.0
        try:
            return float(state.state)
        except ValueError:
            return 0.0
