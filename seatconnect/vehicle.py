#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vehicle class for Seat Connect."""
import re
import time
import logging
import asyncio
import hashlib

from datetime import datetime, timedelta, timezone
from json import dumps as to_json
from collections import OrderedDict
from seatconnect.utilities import find_path, is_valid_path
from seatconnect.exceptions import (
    SeatConfigException,
    SeatException,
    SeatEULAException,
    SeatThrottledException,
    SeatInvalidRequestException,
    SeatRequestInProgressException
)

_LOGGER = logging.getLogger(__name__)

class Vehicle:
    def __init__(self, conn, data):
        _LOGGER.debug(f'Creating Vehicle class object with data {data}')
        self._connection = conn
        self._url = data.get('vin', '')
        self._connectivities = data.get('connectivities', '')
        self._capabilities = data.get('capabilities', [])
        self._specification = data.get('specification', {})
        self._apibase = 'https://msg.volkswagen.de'
        self._secbase = 'https://msg.volkswagen.de'
        self._modelimageurl = None
        self._discovered = False
        self._states = {}

        self._requests = {
            'departuretimer': {'status': '', 'timestamp': datetime.now()},
            'batterycharge': {'status': '', 'timestamp': datetime.now()},
            'climatisation': {'status': '', 'timestamp': datetime.now()},
            'refresh': {'status': '', 'timestamp': datetime.now()},
            'lock': {'status': '', 'timestamp': datetime.now()},
            'honkandflash': {'status': '', 'timestamp': datetime.now()},
            'preheater': {'status': '', 'timestamp': datetime.now()},
            'remaining': -1,
            'latest': '',
            'state': ''
        }
        self._climate_duration = 30

        # API Endpoints that might be enabled for car (that we support)
        self._services = {
            'rheating_v1': {'active': False, 'reason': 'not supported'},
            'rclima_v1': {'active': False, 'reason': 'not supported'},
            'rlu_v1': {'active': False, 'reason': 'not supported'},
            'trip_statistic_v1': {'active': False, 'reason': 'not supported'},
            'statusreport_v1': {'active': False, 'reason': 'not supported'},
            'rbatterycharge_v1': {'active': False, 'reason': 'not supported'},
            'rhonk_v1': {'active': False, 'reason': 'not supported'},
            'carfinder_v1': {'active': False, 'reason': 'not supported'},
            'timerprogramming_v1': {'active': False, 'reason': 'not supported'},
        }

 #### API get and set functions ####
  # Init and update vehicle data
    async def discover(self):
        """Discover vehicle and initial data."""
        homeregion = await self._connection.getHomeRegion(self.vin)
        _LOGGER.debug(f'Get homeregion for VIN {self.vin}')
        if homeregion:
            self._apibase = homeregion.split('/api')[0].replace('mal-', 'fal-') if 'mal-3a' in homeregion else 'https://msg.volkswagen.de'
            self._secbase = homeregion.split('/api')[0]

        await asyncio.gather(
            self.get_realcardata(),
            return_exceptions=True
        )
        _LOGGER.debug(f'Attempting discovery of supported API endpoints for {self.vin}.')
        operationList = await self._connection.getOperationList(self.vin, self._secbase)
        if operationList:
            serviceInfo = operationList['serviceInfo']
            # Iterate over all endpoints in ServiceInfo list
            for service in serviceInfo:
                try:
                    if service.get('serviceId', 'Invalid') in self._services.keys():
                        data = {}
                        serviceName = service.get('serviceId', None)
                        if service.get('serviceStatus', {}).get('status', 'Disabled') == 'Enabled':
                            data['active'] = True
                            if service.get('cumulatedLicense', {}).get('expirationDate', False):
                                data['expiration'] = service.get('cumulatedLicense', {}).get('expirationDate', None).get('content', None)
                            if service.get('operation', False):
                                data.update({'operations': []})
                                for operation in service.get('operation', []):
                                    data['operations'].append(operation.get('id', None))
                            _LOGGER.debug(f'Service {serviceName} is active for {self.vin}: licensed until {data.get("expiration").strftime("%Y-%m-%d %H:%M:%S")}')
                        elif service.get('serviceStatus', {}).get('status', None) == 'Disabled':
                            reason = service.get('serviceStatus', {}).get('reason', 'Unknown')
                            data['active'] = False
                            data['reason'] = reason
                            _LOGGER.debug(f'Service {serviceName} is disabled for {self.vin}: {reason}')
                        else:
                            _LOGGER.warning(f'Could not determine status of service: {serviceName}, assuming enabled')
                            data['active'] = True
                        self._services[serviceName].update(data)
                except Exception as error:
                    _LOGGER.warning(f'Encountered exception: "{error}" while parsing service item: {service}')
                    pass
        else:
            _LOGGER.warning(f'Could not determine available API endpoints for {self.vin}')
        _LOGGER.debug(f'API endpoints: {self._services}')
        if self._connection._session_fulldebug:
            for endpointName, endpoint in self._services.items():
                if endpoint.get('active', False):
                    _LOGGER.debug(f'API endpoint "{endpointName}" valid until {endpoint.get("expiration").strftime("%Y-%m-%d %H:%M:%S")} - operations: {endpoint.get("operations", [])}')

        # Get URL for model image
        self._modelimageurl = await self.get_modelimageurl()

        self._discovered = datetime.now()

    async def update(self):
        """Try to fetch data for all known API endpoints."""
        # Update vehicle information if not discovered or stale information
        if not self._discovered:
            await self.discover()
        else:
            # Rediscover if data is older than 1 hour
            hourago = datetime.now() - timedelta(hours = 1)
            if self._discovered < hourago:
                await self.discover()

        # Fetch all data if car is not deactivated
        if not self.deactivated:
            try:
                await asyncio.gather(
                    self.get_preheater(),
                    self.get_climater(),
                    self.get_trip_statistic(),
                    self.get_position(),
                    self.get_statusreport(),
                    self.get_charger(),
                    self.get_timerprogramming(),
                    return_exceptions=True
                )
            except:
                raise SeatException("Update failed")
            return True
        else:
            _LOGGER.info(f'Vehicle with VIN {self.vin} is deactivated.')
            return False
        return True

  # Data collection functions
    async def get_modelimageurl(self):
        """Fetch the URL for model image."""
        return await self._connection.getModelImageURL(self.vin)

    async def get_realcardata(self):
        """Fetch realcar data."""
        data = await self._connection.getRealCarData()
        if data:
            self._states.update(data)

    async def get_preheater(self):
        """Fetch pre-heater data if function is enabled."""
        if self._services.get('rheating_v1', {}).get('active', False):
            if not await self.expired('rheating_v1'):
                data = await self._connection.getPreHeater(self.vin, self._apibase)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch preheater data')
        else:
            self._requests.pop('preheater', None)
            _LOGGER.info(f'Skipping pre-heater, {self._services.get("rheating_v1", {}).get("reason", "not supported")}')

    async def get_climater(self):
        """Fetch climater data if function is enabled."""
        if self._services.get('rclima_v1', {}).get('active', False):
            if not await self.expired('rclima_v1'):
                data = await self._connection.getClimater(self.vin, self._apibase)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch climater data')
        else:
            self._requests.pop('climatisation', None)
            _LOGGER.info(f'Skipping climatisation, {self._services.get("rclima_v1", {}).get("reason", "not supported")}')

    async def get_trip_statistic(self):
        """Fetch trip data if function is enabled."""
        if self._services.get('trip_statistic_v1', {}).get('active', False):
            if not await self.expired('trip_statistic_v1'):
                data = await self._connection.getTripStatistics(self.vin, self._apibase)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch trip statistics')
        else:
            _LOGGER.info(f'Skipping trip statistics, {self._services.get("trip_statistics_v1", {}).get("reason", "not supported")}')

    async def get_position(self):
        """Fetch position data if function is enabled."""
        if self._services.get('carfinder_v1', {}).get('active', False):
            if not await self.expired('carfinder_v1'):
                data = await self._connection.getPosition(self.vin, self._apibase)
                if data:
                    # Reset requests remaining to 15 if parking time has been updated
                    if data.get('findCarResponse', {}).get('parkingTimeUTC', False):
                        try:
                            newTime = data.get('findCarResponse').get('parkingTimeUTC')
                            oldTime = self.attrs.get('findCarResponse').get('parkingTimeUTC')
                            if newTime > oldTime:
                                self.requests_remaining = 15
                        except:
                            pass
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch any positional data')
        else:
            _LOGGER.info(f'Skipping position, {self._services.get("carfinder_v1", {}).get("reason", "not supported")}')

    async def get_statusreport(self):
        """Fetch status data if function is enabled."""
        if self._services.get('statusreport_v1', {}).get('active', False):
            if not await self.expired('statusreport_v1'):
                data = await self._connection.getVehicleStatusReport(self.vin, self._apibase)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch status report')
        else:
            _LOGGER.info(f'Skipping status report, {self._services.get("statusreport_v1", {}).get("reason", "not supported")}')

    async def get_charger(self):
        """Fetch charger data if function is enabled."""
        if self._services.get('rbatterycharge_v1', {}).get('active', False):
            if not await self.expired('rbatterycharge_v1'):
                data = await self._connection.getCharger(self.vin, self._apibase)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch charger data')
        else:
            self._requests.pop('charger', None)
            _LOGGER.info(f'Skipping charger, {self._services.get("rbatterycharge", {}).get("reason", "not supported")}')

    async def get_timerprogramming(self):
        """Fetch timer data if function is enabled."""
        if self._services.get('timerprogramming_v1', {}).get('active', False):
            if not await self.expired('timerprogramming_v1'):
                data = await self._connection.getDeparturetimer(self.vin, self._apibase)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug('Could not fetch timers')
        else:
            self._requests.pop('departuretimer', None)
            _LOGGER.info(f'Skipping departure timers, {self._services.get("timerprogramming_v1", {}).get("reason", "not supported")}')

    async def wait_for_request(self, section, request, retryCount=36):
        """Update status of outstanding requests."""
        retryCount -= 1
        if (retryCount == 0):
            _LOGGER.info(f'Timeout while waiting for result of {request}.')
            return 'Timeout'
        try:
            status = await self._connection.get_request_status(self.vin, section, request, self._apibase)
            _LOGGER.info(f'Request for {section} with ID {request}: {status}')
            if status == 'In progress':
                self._requests['state'] = 'In progress'
                await asyncio.sleep(5)
                return await self.wait_for_request(section, request, retryCount)
            else:
                self._requests['state'] = status
                return status
        except Exception as error:
            _LOGGER.warning(f'Exception encountered while waiting for request status: {error}')
            return 'Exception'

  # Data set functions
   # API endpoint charging
    async def set_charger_current(self, value):
        """Set charger current"""
        if self.is_charging_supported:
            # Set charger max ampere to integer value
            if isinstance(value, int):
                if 1 <= int(value) <= 255:
                    # VW-Group API charger current request
                    if self._services.get('rbatterycharge_v1', False) is not False:
                        data = {'action': {'settings': {'maxChargeCurrent': int(value)}, 'type': 'setSettings'}}
                else:
                    _LOGGER.error(f'Set charger maximum current to {value} is not supported.')
                    raise SeatInvalidRequestException(f'Set charger maximum current to {value} is not supported.')
            # Mimick app and set charger max ampere to Maximum/Reduced
            elif isinstance(value, str):
                if value in ['Maximum', 'maximum', 'Max', 'max', 'Minimum', 'minimum', 'Min', 'min', 'Reduced', 'reduced']:
                    # VW-Group API charger current request
                    if self._services.get('rbatterycharge_v1', False) is not False:
                        value = 254 if value in ['Maximum', 'maximum', 'Max', 'max'] else 252
                        data = {'action': {'settings': {'maxChargeCurrent': int(value)}, 'type': 'setSettings'}}
                else:
                    _LOGGER.error(f'Set charger maximum current to {value} is not supported.')
                    raise SeatInvalidRequestException(f'Set charger maximum current to {value} is not supported.')
            else:
                _LOGGER.error(f'Data type passed is invalid.')
                raise SeatInvalidRequestException(f'Invalid data type.')
            return await self.set_charger(data)
        else:
            _LOGGER.error('No charger support.')
            raise SeatInvalidRequestException('No charger support.')

    async def set_charger(self, action):
        """Charging actions."""
        if not self._services.get('rbatterycharge_v1', False) and not self._services.get('CHARGING', False):
            _LOGGER.info('Remote start/stop of charger is not supported.')
            raise SeatInvalidRequestException('Remote start/stop of charger is not supported.')
        if self._requests['batterycharge'].get('id', False):
            timestamp = self._requests.get('batterycharge', {}).get('timestamp', datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('batterycharge', {}).pop('id')
            else:
                raise SeatRequestInProgressException('Charging action already in progress')
        # VW-Group API requests
        if self._services.get('rbatterycharge_v1', False):
            if action in ['start', 'Start', 'On', 'on']:
                data = {'action': {'type': 'start'}}
            elif action in ['stop', 'Stop', 'Off', 'off']:
                data = {'action': {'type': 'stop'}}
            elif isinstance(action.get('action', None), dict):
                data = action
            else:
                _LOGGER.error(f'Invalid charger action: {action}. Must be either start, stop or setSettings')
                raise SeatInvalidRequestException(f'Invalid charger action: {action}. Must be either start, stop or setSettings')
        try:
            self._requests['latest'] = 'Charger'
            response = await self._connection.setCharger(self.vin, self._apibase, data)
            if not response:
                self._requests['batterycharge'] = {'status': 'Failed'}
                _LOGGER.error(f'Failed to {action} charging')
                raise SeatException(f'Failed to {action} charging')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['batterycharge'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('state', 'Unknown'),
                    'id': response.get('id', 0)
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('batterycharge', response.get('id', 0))
                self._requests['batterycharge'] = {'status': status}
                return True
        except (SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to {action} charging - {error}')
            self._requests['batterycharge'] = {'status': 'Exception'}
            raise SeatException(f'Failed to execute set charger - {error}')

   # API endpoint departuretimer
    async def set_charge_limit(self, limit=50):
        """ Set charging limit. """
        if not self._services.get('timerprogramming_v1', False) and not self._services.get('CHARGING', False):
            _LOGGER.info('Set charging limit is not supported.')
            raise SeatInvalidRequestException('Set charging limit is not supported.')
        data = {}
        # VW-Group API charging
        if self._services.get('timerprogramming_v1', False) is not False:
            if isinstance(limit, int):
                if limit in [0, 10, 20, 30, 40, 50]:
                    data['limit'] = limit
                    data['action'] = 'chargelimit'
                else:
                    raise SeatInvalidRequestException(f'Charge limit must be one of 0, 10, 20, 30, 40 or 50.')
            else:
                raise SeatInvalidRequestException(f'Charge limit "{limit}" is not supported.')
            return await self._set_timers(data)

    async def set_timer_active(self, id=1, action='off'):
        """ Activate/deactivate departure timers. """
        data = {}
        supported = 'is_departure' + str(id) + "_supported"
        if getattr(self, supported) is not True:
            raise SeatConfigException(f'This vehicle does not support timer id "{id}".')
        # VW-Group API
        if self._services.get('timerprogramming_v1', False):
            data['id'] = id
            if action in ['on', 'off']:
                data['action'] = action
            else:
                raise SeatInvalidRequestException(f'Timer action "{action}" is not supported.')
            return await self._set_timers(data)
        else:
            raise SeatInvalidRequestException('Departure timers are not supported.')

    async def set_timer_schedule(self, id, schedule={}):
        """ Set departure schedules. """
        data = {}
        # Validate required user inputs
        supported = 'is_departure' + str(id) + "_supported"
        if getattr(self, supported) is not True:
            raise SeatConfigException(f'Timer id "{id}" is not supported for this vehicle.')
        else:
            _LOGGER.debug(f'Timer id {id} is supported')
        if not schedule:
            raise SeatInvalidRequestException('A schedule must be set.')
        if not isinstance(schedule.get('enabled', ''), bool):
            raise SeatInvalidRequestException('The enabled variable must be set to True or False.')
        if not isinstance(schedule.get('recurring', ''), bool):
            raise SeatInvalidRequestException('The recurring variable must be set to True or False.')
        if not re.match('^[0-9]{2}:[0-9]{2}$', schedule.get('time', '')):
            raise SeatInvalidRequestException('The time for departure must be set in 24h format HH:MM.')

        # Validate optional inputs
        if schedule.get('recurring', False):
            if not re.match('^[yn]{7}$', schedule.get('days', '')):
                raise SeatInvalidRequestException('For recurring schedules the days variable must be set to y/n mask (mon-sun with only wed enabled): nnynnnn.')
        elif not schedule.get('recurring'):
            if not re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}$', schedule.get('date', '')):
                raise SeatInvalidRequestException('For single departure schedule the date variable must be set to YYYY-mm-dd.')

        # VW-Group API
        if self._services.get('timerprogramming_v1', False):
            # Validate options only available for VW-Group API
            # Sanity check for off-peak hours
            if not isinstance(schedule.get('nightRateActive', False), bool):
                raise SeatInvalidRequestException('The off-peak active variable must be set to True or False')
            if schedule.get('nightRateStart', None) is not None:
                if not re.match('^[0-9]{2}:[0-9]{2}$', schedule.get('nightRateStart', '')):
                    raise SeatInvalidRequestException('The start time for off-peak hours must be set in 24h format HH:MM.')
            if schedule.get('nightRateEnd', None) is not None:
                if not re.match('^[0-9]{2}:[0-9]{2}$', schedule.get('nightRateEnd', '')):
                    raise SeatInvalidRequestException('The start time for off-peak hours must be set in 24h format HH:MM.')

            # Check if charging/climatisation is set and correct
            if not isinstance(schedule.get('operationClimatisation', False), bool):
                raise SeatInvalidRequestException('The climatisation enable variable must be set to True or False')
            if not isinstance(schedule.get('operationCharging', False), bool):
                raise SeatInvalidRequestException('The charging variable must be set to True or False')

            # Validate temp setting, if set
            if schedule.get("targetTemp", None) is not None:
                if not 16 <= int(schedule.get("targetTemp", None)) <= 30:
                    raise SeatInvalidRequestException('Target temp must be integer value from 16 to 30')
                else:
                    data['temp'] = schedule.get('targetTemp')

            # Validate charge target and current
            if schedule.get("targetChargeLevel", None) is not None:
                if not 0 <= int(schedule.get("targetChargeLevel", None)) <= 100:
                    raise SeatInvalidRequestException('Target charge level must be 0 to 100')
            if schedule.get("chargeMaxCurrent", None) is not None:
                if isinstance(schedule.get('chargeMaxCurrent', None), str):
                    if not schedule.get("chargeMaxCurrent", None) in ['Maximum', 'maximum', 'Max', 'max', 'Minimum', 'minimum', 'Min', 'min', 'Reduced', 'reduced']:
                        raise SeatInvalidRequestException('Charge current must be one of Maximum/Minimum/Reduced')
                elif isinstance(schedule.get('chargeMaxCurrent', None), int):
                    if not 1 <= int(schedule.get("chargeMaxCurrent", 254)) < 255:
                        raise SeatInvalidRequestException('Charge current must be set from 1 to 254')
                else:
                    raise SeatInvalidRequestException('Invalid type for charge max current variable')
            # Prepare data and execute
            data['id'] = id
            data['action'] = 'schedule'
            data['schedule'] = schedule
            return await self._set_timers(data)
        else:
            _LOGGER.info('Departure timers are not supported.')
            raise SeatInvalidRequestException('Departure timers are not supported.')

    async def _set_timers(self, data=None):
        """ Set departure timers. """
        if not self._services.get('timerprogramming_v1', False):
            raise SeatInvalidRequestException('Departure timers are not supported.')
        if self._requests['departuretimer'].get('id', False):
            timestamp = self._requests.get('departuretimer', {}).get('timestamp', datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('departuretimer', {}).pop('id')
            else:
                raise SeatRequestInProgressException('Scheduling of departure timer is already in progress')
        # Verify temperature setting
        if data.get('temp', False):
            if data['temp'] in {16,16.5,17,17.5,18,18.5,19,19.5,20,20.5,21,21.5,22,22.5,23,23.5,24,24.5,25,25.5,26,26.5,27,27.5,28,28.5,29,29.5,30}:
                data['temp'] = int((data['temp'] + 273) * 10)
            else:
                data['temp'] = int((int(data['temp']) + 273) * 10)
        else:
            try:
                data['temp'] = int((self.climatisation_target_temperature + 273) * 10)
            except:
                data['temp'] = 2930
                pass
        if 2890 <= data['temp'] <= 3030:
            pass
        else:
            data['temp'] = 2930

        try:
            self._requests['latest'] = 'Departuretimer'
            response = await self._connection.setDeparturetimer(self.vin, self._apibase, data, spin=False)
            if not response:
                self._requests['departuretimer'] = {'status': 'Failed'}
                _LOGGER.error('Failed to execute departure timer request')
                raise SeatException('Failed to execute departure timer request')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['departuretimer'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('state', 'Unknown'),
                    'id': response.get('id', 0),
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('departuretimer', response.get('id', 0))
                self._requests['departuretimer'] = {'status': status}
                return True
        except (SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to execute departure timer request - {error}')
            self._requests['departuretimer'] = {'status': 'Exception'}
        raise SeatException('Failed to set departure timer schedule')

   # Climatisation electric/auxiliary/windows (CLIMATISATION)
    async def set_climatisation_temp(self, temperature=20):
        """Set climatisation target temp."""
        if self.is_electric_climatisation_supported or self.is_auxiliary_climatisation_supported:
            if 16 <= int(temperature) <= 30:
                temp = int((temperature + 273) * 10)
                data = {'action': {'settings': {'targetTemperature': temp}, 'type': 'setSettings'}}
            else:
                _LOGGER.error(f'Set climatisation target temp to {temperature} is not supported.')
                raise SeatInvalidRequestException(f'Set climatisation target temp to {temperature} is not supported.')
            return await self._set_climater(data)
        else:
            _LOGGER.error('No climatisation support.')
            raise SeatInvalidRequestException('No climatisation support.')

    async def set_window_heating(self, action = 'stop'):
        """Turn on/off window heater."""
        if self.is_window_heater_supported:
            if action in ['start', 'stop']:
                data = {'action': {'type': action + 'WindowHeating'}}
            else:
                _LOGGER.error(f'Window heater action "{action}" is not supported.')
                raise SeatInvalidRequestException(f'Window heater action "{action}" is not supported.')
            return await self._set_climater(data)
        else:
            _LOGGER.error('No climatisation support.')
            raise SeatInvalidRequestException('No climatisation support.')

    async def set_battery_climatisation(self, mode = False):
        """Turn on/off electric climatisation from battery."""
        if self.is_electric_climatisation_supported:
            if mode in [True, False]:
                data = {'action': {'settings': {'climatisationWithoutHVpower': mode}, 'type': 'setSettings'}}
            else:
                _LOGGER.error(f'Set climatisation without external power to "{mode}" is not supported.')
                raise SeatInvalidRequestException(f'Set climatisation without external power to "{mode}" is not supported.')
            return await self._set_climater(data)
        else:
            _LOGGER.error('No climatisation support.')
            raise SeatInvalidRequestException('No climatisation support.')

    async def set_climatisation(self, mode = 'off', temp = None, hvpower = None, spin = None):
        """Turn on/off climatisation with electric/auxiliary heater."""
        # Validate user input
        if mode not in ['electric', 'auxiliary', 'Start', 'Stop', 'on', 'off']:
            raise SeatInvalidRequestException(f"Invalid mode for set_climatisation: {mode}")
        elif mode == 'auxiliary' and spin is None:
            raise SeatInvalidRequestException("Starting auxiliary heater requires provided S-PIN")
        if temp is not None:
            if not isinstance(temp, int):
                raise SeatInvalidRequestException(f"Invalid type for temp")
            elif not 16 <= int(temp) <=30:
                raise SeatInvalidRequestException(f"Invalid value for temp")
        else:
            temp = self.climatisation_target_temperature
        if hvpower is not None:
            if not isinstance(hvpower, bool):
                raise SeatInvalidRequestException(f"Invalid type for hvpower")
        if self.is_electric_climatisation_supported:
            if self._services.get('rclima_v1', False):
                if mode in ['Start', 'start', 'On', 'on']:
                    mode = 'electric'
                if mode in ['electric', 'auxiliary']:
                    targetTemp = int((temp + 273) * 10)
                    if hvpower is not None:
                        withoutHVPower = hvpower
                    else:
                        withoutHVPower = self.climatisation_without_external_power
                    data = {
                        'action':{
                            'settings':{
                                'climatisationWithoutHVpower': withoutHVPower,
                                'targetTemperature': targetTemp,
                                'heaterSource': mode
                            },
                            'type': 'startClimatisation'
                        }
                    }
                else:
                    data = {'action': {'type': 'stopClimatisation'}}
                return await self._set_climater(data, spin)
        else:
            _LOGGER.error('No climatisation support.')
        raise SeatInvalidRequestException('No climatisation support.')

    async def _set_climater(self, data, spin = False):
        """Climater actions."""
        if not self._services.get('rclima_v1', False):
            _LOGGER.info('Remote control of climatisation functions is not supported.')
            raise SeatInvalidRequestException('Remote control of climatisation functions is not supported.')
        if self._requests['climatisation'].get('id', False):
            timestamp = self._requests.get('climatisation', {}).get('timestamp', datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('climatisation', {}).pop('id')
            else:
                raise SeatRequestInProgressException('A climatisation action is already in progress')
        try:
            self._requests['latest'] = 'Climatisation'
            response = await self._connection.setClimater(self.vin, self._apibase, data, spin)
            if not response:
                self._requests['climatisation'] = {'status': 'Failed'}
                _LOGGER.error('Failed to execute climatisation request')
                raise SeatException('Failed to execute climatisation request')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['climatisation'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('state', 'Unknown'),
                    'id': response.get('id', 0),
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('climatisation', response.get('id', 0))
                self._requests['climatisation'] = {'status': status}
                return True
        except (SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to execute climatisation request - {error}')
            self._requests['climatisation'] = {'status': 'Exception'}
        raise SeatException('Climatisation action failed')

   # Parking heater heating/ventilation (RS)
    async def set_pheater(self, mode, spin):
        """Set the mode for the parking heater."""
        if not self.is_pheater_heating_supported:
            _LOGGER.error('No parking heater support.')
            raise SeatInvalidRequestException('No parking heater support.')
        if self._requests['preheater'].get('id', False):
            timestamp = self._requests.get('preheater', {}).get('timestamp', datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('preheater', {}).pop('id')
            else:
                raise SeatRequestInProgressException('A parking heater action is already in progress')
        if not mode in ['heating', 'ventilation', 'off']:
            _LOGGER.error(f'{mode} is an invalid action for parking heater')
            raise SeatInvalidRequestException(f'{mode} is an invalid action for parking heater')
        if mode == 'off':
            data = {'performAction': {'quickstop': {'active': False }}}
        else:
            data = {'performAction': {'quickstart': {'climatisationDuration': self.pheater_duration, 'startMode': mode, 'active': True }}}
        try:
            self._requests['latest'] = 'Preheater'
            _LOGGER.debug(f'Executing setPreHeater with data: {data}')
            response = await self._connection.setPreHeater(self.vin, self._apibase, data, spin)
            if not response:
                self._requests['preheater'] = {'status': 'Failed'}
                _LOGGER.error(f'Failed to set parking heater to {mode}')
                raise SeatException(f'setPreHeater returned "{response}"')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['preheater'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('state', 'Unknown'),
                    'id': response.get('id', 0),
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('rs', response.get('id', 0))
                self._requests['preheater'] = {'status': status}
                return True
        except (SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to set parking heater mode to {mode} - {error}')
            self._requests['preheater'] = {'status': 'Exception'}
        raise SeatException('Pre-heater action failed')

   # Lock (RLU)
    async def set_lock(self, action, spin):
        """Remote lock and unlock actions."""
        if not self._services.get('rlu_v1', False):
            _LOGGER.info('Remote lock/unlock is not supported.')
            raise SeatInvalidRequestException('Remote lock/unlock is not supported.')
        if self._requests['lock'].get('id', False):
            timestamp = self._requests.get('lock', {}).get('timestamp', datetime.now() - timedelta(minutes=5))
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('lock', {}).pop('id')
            else:
                raise SeatRequestInProgressException('A lock action is already in progress')
        if action in ['lock', 'unlock']:
            data = '<rluAction xmlns="http://audi.de/connect/rlu">\n<action>' + action + '</action>\n</rluAction>'
        else:
            _LOGGER.error(f'Invalid lock action: {action}')
            raise SeatInvalidRequestException(f'Invalid lock action: {action}')
        try:
            self._requests['latest'] = 'Lock'
            response = await self._connection.setLock(self.vin, self._apibase, data, spin)
            if not response:
                self._requests['lock'] = {'status': 'Failed'}
                _LOGGER.error(f'Failed to {action} vehicle')
                raise SeatException(f'Failed to {action} vehicle')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['lock'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('state', 'Unknown'),
                    'id': response.get('id', 0),
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('rlu', response.get('id', 0))
                self._requests['lock'] = {'status': status}
                return True
        except (SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to {action} vehicle - {error}')
            self._requests['lock'] = {'status': 'Exception'}
        raise SeatException('Lock action failed')

   # Honk and flash (RHF)
    async def set_honkandflash(self, action, lat=None, lng=None):
        """Turn on/off honk and flash."""
        if not self._services.get('rhonk_v1', False):
            _LOGGER.info('Remote honk and flash is not supported.')
            raise SeatInvalidRequestException('Remote honk and flash is not supported.')
        if self._requests['honkandflash'].get('id', False):
            timestamp = self._requests.get('honkandflash', {}).get('timestamp', datetime.now() - timedelta(minutes=5))
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('honkandflash', {}).pop('id')
            else:
                raise SeatRequestInProgressException('A honk and flash action is already in progress')
        if action == 'flash':
            operationCode = 'FLASH_ONLY'
        elif action == 'honkandflash':
            operationCode = 'HONK_AND_FLASH'
        else:
            raise SeatInvalidRequestException(f'Invalid action "{action}", must be one of "flash" or "honkandflash"')
        try:
            # Get car position
            if lat is None:
                lat = int(self.attrs.get('findCarResponse', {}).get('Position', {}).get('carCoordinate', {}).get('latitude', None))
            if lng is None:
                lng = int(self.attrs.get('findCarResponse', {}).get('Position', {}).get('carCoordinate', {}).get('longitude', None))
            if lat is None or lng is None:
                raise SeatConfigException('No location available, location information is needed for this action')
            data = {
                'honkAndFlashRequest': {
                    'serviceOperationCode': operationCode,
                    'userPosition': {
                        'latitude': lat,
                        'longitude': lng
                    }
                }
            }
            self._requests['latest'] = 'HonkAndFlash'
            response = await self._connection.setHonkAndFlash(self.vin, self._apibase, data)
            if not response:
                self._requests['honkandflash'] = {'status': 'Failed'}
                _LOGGER.error(f'Failed to execute honk and flash action')
                raise SeatException(f'Failed to execute honk and flash action')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['honkandflash'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('state', 'Unknown'),
                    'id': response.get('id', 0),
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('rhf', response.get('id', 0))
                self._requests['honkandflash'] = {'status': status}
                return True
        except (SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to {action} vehicle - {error}')
            self._requests['honkandflash'] = {'status': 'Exception'}
        raise SeatException('Honk and flash action failed')

   # Refresh vehicle data (VSR)
    async def set_refresh(self):
        """Wake up vehicle and update status data."""
        if not self._services.get('statusreport_v1', {}).get('active', False):
           _LOGGER.info('Data refresh is not supported.')
           raise SeatInvalidRequestException('Data refresh is not supported.')
        if self._requests['refresh'].get('id', False):
            timestamp = self._requests.get('refresh', {}).get('timestamp', datetime.now() - timedelta(minutes=5))
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get('refresh', {}).pop('id')
            else:
                raise SeatRequestInProgressException('A data refresh request is already in progress')
        try:
            self._requests['latest'] = 'Refresh'
            response = await self._connection.setRefresh(self.vin, self._apibase)
            if not response:
                _LOGGER.error('Failed to request vehicle update')
                self._requests['refresh'] = {'status': 'Failed'}
                raise SeatException('Failed to execute data refresh')
            else:
                self._requests['remaining'] = response.get('rate_limit_remaining', -1)
                self._requests['refresh'] = {
                    'timestamp': datetime.now(),
                    'status': response.get('status', 'Unknown'),
                    'id': response.get('id', 0)
                }
                if response.get('state', None) == 'Throttled':
                    status = 'Throttled'
                else:
                    status = await self.wait_for_request('vsr', response.get('id', 0))
                self._requests['refresh'] = {
                    'status': status
                }
                return True
        except(SeatInvalidRequestException, SeatException):
            raise
        except Exception as error:
            _LOGGER.warning(f'Failed to execute data refresh - {error}')
            self._requests['refresh'] = {'status': 'Exception'}
        raise SeatException('Data refresh failed')

 #### Vehicle class helpers ####
  # Vehicle info
    @property
    def attrs(self):
        return self._states

    def has_attr(self, attr):
        return is_valid_path(self.attrs, attr)

    def get_attr(self, attr):
        return find_path(self.attrs, attr)

    async def expired(self, service):
        """Check if access to service has expired. Return true if expired."""
        try:
            now = datetime.utcnow()
            if self._services.get(service, {}).get('expiration', False):
                expiration = self._services.get(service, {}).get('expiration', False)
                if not expiration:
                    expiration = datetime.utcnow() + timedelta(days = 1)
            else:
                _LOGGER.debug(f'Could not determine end of access for service {service}, assuming it is valid')
                expiration = datetime.utcnow() + timedelta(days = 1)
            expiration = expiration.replace(tzinfo = None)
            if now >= expiration:
                _LOGGER.warning(f'Access to {service} has expired!')
                self._discovered = False
                return True
            else:
                return False
        except:
            _LOGGER.debug(f'Exception. Could not determine end of access for service {service}, assuming it is valid')
            return False

    def dashboard(self, **config):
        #Classic python notation
        from seatconnect.dashboard import Dashboard
        return Dashboard(self, **config)

    @property
    def vin(self):
        return self._url

    @property
    def unique_id(self):
        return self.vin


 #### Information from vehicle states ####
  # Car information
    @property
    def nickname(self):
        for car in self.attrs.get('realCars', []):
            if self.vin == car.get('vehicleIdentificationNumber', ''):
                return car.get('nickname', None)

    @property
    def is_nickname_supported(self):
        for car in self.attrs.get('realCars', []):
            if self.vin == car.get('vehicleIdentificationNumber', ''):
                if car.get('nickname', False):
                    return True

    @property
    def deactivated(self):
        for car in self.attrs.get('realCars', []):
            if self.vin == car.get('vehicleIdentificationNumber', ''):
                return car.get('deactivated', False)

    @property
    def is_deactivated_supported(self):
        for car in self.attrs.get('realCars', []):
            if self.vin == car.get('vehicleIdentificationNumber', ''):
                if car.get('deactivated', False):
                    return True

    @property
    def model(self):
        """Return model"""
        if self._specification.get('trimLevel', False):
            model = self._specification.get('title', 'Unknown') + ' ' + self._specification.get('trimLevel', '')
            return model
        return self._specification.get('title', 'Unknown')

    @property
    def is_model_supported(self):
        """Return true if model is supported."""
        if self._specification.get('title', False):
            return True

    @property
    def model_year(self):
        """Return model year"""
        return self._specification.get('manufacturingDate', 'Unknown')

    @property
    def is_model_year_supported(self):
        """Return true if model year is supported."""
        if self._specification.get('manufacturingDate', False):
            return True

    @property
    def model_image(self):
        """Return URL for model image"""
        return self._modelimageurl

    @property
    def is_model_image_supported(self):
        """Return true if model image url is not None."""
        if self._modelimageurl is not None:
            return True

  # Lights
    @property
    def parking_light(self):
        """Return true if parking light is on"""
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301010001'].get('value', 0))
        if response != 2:
            return True
        else:
            return False

    @property
    def is_parking_light_supported(self):
        """Return true if parking light is supported"""
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301010001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
            else:
                return False

  # Connection status
    @property
    def last_connected(self):
        """Return when vehicle was last connected to connect servers."""
        last_connected_utc = self.attrs.get('StoredVehicleDataResponse').get('vehicleData').get('data')[0].get('field')[0].get('tsCarSentUtc')
        if isinstance(last_connected_utc, datetime):
            last_connected = last_connected_utc.replace(tzinfo=timezone.utc).astimezone(tz=None)
        else:
            last_connected = datetime.strptime(last_connected_utc,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).astimezone(tz=None)
        return last_connected.strftime('%Y-%m-%d %H:%M:%S')

    @property
    def is_last_connected_supported(self):
        """Return when vehicle was last connected to connect servers."""
        if next(iter(next(iter(self.attrs.get('StoredVehicleDataResponse', {}).get('vehicleData', {}).get('data', {})), None).get('field', {})), None).get('tsCarSentUtc', []):
            return True

  # Service information
    @property
    def distance(self):
        """Return vehicle odometer."""
        value = self.attrs.get('StoredVehicleDataResponseParsed')['0x0101010002'].get('value', 0)
        return int(value)

    @property
    def is_distance_supported(self):
        """Return true if odometer is supported"""
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0101010002' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
        return False

    @property
    def service_inspection(self):
        """Return time left until service inspection"""
        value = -1
        value = 0-int(self.attrs.get('StoredVehicleDataResponseParsed', {}).get('0x0203010004',{}).get('value', 0))
        return int(value)

    @property
    def is_service_inspection_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0203010004' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
        return False

    @property
    def service_inspection_distance(self):
        """Return time left until service inspection"""
        value = -1
        value = 0-int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0203010003'].get('value', 0))
        return int(value)

    @property
    def is_service_inspection_distance_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0203010003' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
        return False

    @property
    def oil_inspection(self):
        """Return time left until oil inspection"""
        value = -1
        value = 0-int(self.attrs.get('StoredVehicleDataResponseParsed', {}).get('0x0203010002', {}).get('value', 0))
        return int(value)

    @property
    def is_oil_inspection_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0203010002' in self.attrs.get('StoredVehicleDataResponseParsed'):
                if self.attrs.get('StoredVehicleDataResponseParsed').get('0x0203010002').get('value', None) is not None:
                    return True
        return False

    @property
    def oil_inspection_distance(self):
        """Return distance left until oil inspection"""
        value = -1
        value = 0-int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0203010001'].get('value', 0))
        return int(value)

    @property
    def is_oil_inspection_distance_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0203010001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                if self.attrs.get('StoredVehicleDataResponseParsed').get('0x0203010001').get('value', None) is not None:
                    return True
        return False

    @property
    def adblue_level(self):
        """Return adblue level."""
        return int(self.attrs.get('StoredVehicleDataResponseParsed', {}).get('0x02040C0001', {}).get('value', 0))

    @property
    def is_adblue_level_supported(self):
        """Return true if adblue level is supported."""
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x02040C0001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                if 'value' in self.attrs.get('StoredVehicleDataResponseParsed')['0x02040C0001']:
                    if self.attrs.get('StoredVehicleDataResponseParsed')['0x02040C0001'].get('value', 0) is not None:
                        return True
        return False

  # Charger related states for EV and PHEV
    @property
    def charging(self):
        """Return battery level"""
        cstate = self.attrs.get('charger').get('status').get('chargingStatusData').get('chargingState').get('content', '')
        return 1 if cstate in ['charging', 'Charging'] else 0

    @property
    def is_charging_supported(self):
        """Return true if charging is supported"""
        if self.attrs.get('charger', False):
            if 'status' in self.attrs.get('charger', {}):
                if 'chargingStatusData' in self.attrs.get('charger')['status']:
                    if 'chargingState' in self.attrs.get('charger')['status']['chargingStatusData']:
                        return True
        return False

    @property
    def min_charge_level(self):
        """Return the charge level that car charges directly to"""
        if self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerBasicSetting', {}).get('chargeMinLimit', False):
            return self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerBasicSetting', {}).get('chargeMinLimit', 0)
        else:
            return 0

    @property
    def is_min_charge_level_supported(self):
        """Return true if car supports setting the min charge level"""
        if self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerBasicSetting', {}).get('chargeMinLimit', False):
            return True
        return False

    @property
    def battery_level(self):
        """Return battery level"""
        if self.attrs.get('charger', False):
            return int(self.attrs.get('charger').get('status', {}).get('batteryStatusData', {}).get('stateOfCharge', {}).get('content', 0))
        else:
            return 0

    @property
    def is_battery_level_supported(self):
        """Return true if battery level is supported"""
        if self.attrs.get('charger', False):
            if 'status' in self.attrs.get('charger'):
                if 'batteryStatusData' in self.attrs.get('charger')['status']:
                    if 'stateOfCharge' in self.attrs.get('charger')['status']['batteryStatusData']:
                        return True
        return False

    @property
    def charge_max_ampere(self):
        """Return charger max ampere setting."""
        if self.attrs.get('charger', False):
            value = int(self.attrs.get('charger').get('settings').get('maxChargeCurrent').get('content'))
            if value == 254:
                return "Maximum"
            if value == 252:
                return "Reduced"
            if value == 0:
                return "Unknown"
            else:
                return value
        return 0

    @property
    def is_charge_max_ampere_supported(self):
        """Return true if Charger Max Ampere is supported"""
        if self.attrs.get('charger', False):
            if 'settings' in self.attrs.get('charger', {}):
                if 'maxChargeCurrent' in self.attrs.get('charger', {})['settings']:
                    return True
        return False

    @property
    def charging_cable_locked(self):
        """Return plug locked state"""
        response = ''
        if self.attrs.get('charger', False):
            response = self.attrs.get('charger')['status']['plugStatusData']['lockState'].get('content', 0)
        return True if response in ['Locked', 'locked'] else False

    @property
    def is_charging_cable_locked_supported(self):
        """Return true if plug locked state is supported"""
        if self.attrs.get('charger', False):
            if 'status' in self.attrs.get('charger', {}):
                if 'plugStatusData' in self.attrs.get('charger').get('status', {}):
                    if 'lockState' in self.attrs.get('charger')['status'].get('plugStatusData', {}):
                        return True
        return False

    @property
    def charging_cable_connected(self):
        """Return plug locked state"""
        response = ''
        if self.attrs.get('charger', False):
            response = self.attrs.get('charger', {}).get('status', {}).get('plugStatusData').get('plugState', {}).get('content', 0)
        return True if response in ['Connected', 'connected'] else False

    @property
    def is_charging_cable_connected_supported(self):
        """Return true if charging cable connected is supported"""
        if self.attrs.get('charger', False):
            if 'status' in self.attrs.get('charger', {}):
                if 'plugStatusData' in self.attrs.get('charger').get('status', {}):
                    if 'plugState' in self.attrs.get('charger')['status'].get('plugStatusData', {}):
                        return True
        return False

    @property
    def charging_time_left(self):
        """Return minutes to charging complete"""
        if self.external_power:
            if self.attrs.get('charging', {}).get('remainingToCompleteInSeconds', False):
                minutes = int(self.attrs.get('charging', {}).get('remainingToCompleteInSeconds', 0))/60
            elif self.attrs.get('charger', {}).get('status', {}).get('batteryStatusData', {}).get('remainingChargingTime', False):
                minutes = self.attrs.get('charger', {}).get('status', {}).get('batteryStatusData', {}).get('remainingChargingTime', {}).get('content', 0)
            try:
                if minutes == -1: return '00:00'
                if minutes == 65535: return '00:00'
                return "%02d:%02d" % divmod(minutes, 60)
            except Exception:
                pass
        return '00:00'

    @property
    def is_charging_time_left_supported(self):
        """Return true if charging is supported"""
        return self.is_charging_supported

    @property
    def charging_power(self):
        """Return charging power in watts."""
        if self.attrs.get('charging', False):
            return int(self.attrs.get('charging', {}).get('chargingPowerInWatts', 0))
        else:
            return 0

    @property
    def is_charging_power_supported(self):
        """Return true if charging power is supported."""
        if self.attrs.get('charging', False):
            if self.attrs.get('charging', {}).get('chargingPowerInWatts', False) is not False:
                return True
        return False

    @property
    def charge_rate(self):
        """Return charge rate in km per h."""
        if self.attrs.get('charging', False):
            return int(self.attrs.get('charging', {}).get('chargingRateInKilometersPerHour', 0))
        else:
            return 0

    @property
    def is_charge_rate_supported(self):
        """Return true if charge rate is supported."""
        if self.attrs.get('charging', False):
            if self.attrs.get('charging', {}).get('chargingRateInKilometersPerHour', False) is not False:
                return True
        return False

    @property
    def external_power(self):
        """Return true if external power is connected."""
        response = ''
        if self.attrs.get('charger', False):
            response = self.attrs.get('charger', {}).get('status', {}).get('chargingStatusData', {}).get('externalPowerSupplyState', {}).get('content', 0)
        elif self.attrs.get('charging', False):
            response = self.attrs.get('charging', {}).get('chargingType', 'Invalid')
            response = 'Charging' if self.attrs.get('charging', {}).get('chargingType', 'Invalid') != 'Invalid' else 'Invalid'
        return True if response in ['stationConnected', 'available', 'Charging'] else False

    @property
    def is_external_power_supported(self):
        """External power supported."""
        if self.attrs.get('charger', {}).get('status', {}).get('chargingStatusData', {}).get('externalPowerSupplyState', False):
            return True
        if self.attrs.get('charging', {}).get('chargingType', False):
            return True

    @property
    def energy_flow(self):
        """Return true if energy is flowing through charging port."""
        check = self.attrs.get('charger', {}).get('status', {}).get('chargingStatusData', {}).get('energyFlow', {}).get('content', 'off')
        if check == 'on':
            return True
        else:
            return False

    @property
    def is_energy_flow_supported(self):
        """Energy flow supported."""
        if self.attrs.get('charger', {}).get('status', {}).get('chargingStatusData', {}).get('energyFlow', False):
            return True

  # Vehicle location states
    @property
    def position(self):
        """Return  position."""
        output = {}
        try:
            if self.vehicle_moving:
                output = {
                    'lat': None,
                    'lng': None,
                    'timestamp': None
                }
            else:
                posObj = self.attrs.get('findCarResponse', {})
                lat = int(posObj.get('Position').get('carCoordinate').get('latitude'))/1000000
                lng = int(posObj.get('Position').get('carCoordinate').get('longitude'))/1000000
                parkingTime = posObj.get('parkingTimeUTC')
                output = {
                    'lat' : lat,
                    'lng' : lng,
                    'timestamp' : parkingTime
                }
        except:
            output = {
                'lat': '?',
                'lng': '?',
            }
        return output

    @property
    def is_position_supported(self):
        """Return true if carfinder_v1 service is active."""
        if self._services.get('carfinder_v1', {}).get('active', False):
        #if self.attrs.get('findCarResponse', {}).get('Position', {}).get('carCoordinate', {}).get('latitude', False):
            return True
        elif self.attrs.get('isMoving', False):
            return True

    @property
    def vehicle_moving(self):
        """Return true if vehicle is moving."""
        return self.attrs.get('isMoving', False)

    @property
    def is_vehicle_moving_supported(self):
        """Return true if vehicle supports position."""
        if self.is_position_supported:
            return True

    @property
    def parking_time(self):
        """Return timestamp of last parking time."""
        parkTime_utc = self.attrs.get('findCarResponse', {}).get('parkingTimeUTC', 'Unknown')
        if isinstance(parkTime_utc, datetime):
            parkTime = parkTime_utc.replace(tzinfo=timezone.utc).astimezone(tz=None)
        else:
            parkTime = datetime.strptime(parkTime_utc,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).astimezone(tz=None)
        return parkTime.strftime('%Y-%m-%d %H:%M:%S')

    @property
    def is_parking_time_supported(self):
        """Return true if vehicle parking timestamp is supported."""
        if 'parkingTimeUTC' in self.attrs.get('findCarResponse', {}):
            return True

  # Vehicle fuel level and range
    @property
    def electric_range(self):
        value = -1
        if '0x0301030008' in self.attrs.get('StoredVehicleDataResponseParsed', {}):
            if 'value' in self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030008']:
                value = self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030008'].get('value', 0)
        elif self.attrs.get('battery', False):
            value = int(self.attrs.get('battery', {}).get('cruisingRangeElectricInMeters', 0))/1000
        return int(value)

    @property
    def is_electric_range_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301030008' in self.attrs.get('StoredVehicleDataResponseParsed'):
                if 'value' in self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030008']:
                    return True
        return False

    @property
    def combustion_range(self):
        value = -1
        if '0x0301030006' in self.attrs.get('StoredVehicleDataResponseParsed'):
            if 'value' in self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030006']:
                value = self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030006'].get('value', 0)
        return int(value)

    @property
    def is_combustion_range_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301030006' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
        return False

    @property
    def combined_range(self):
        value = -1
        if '0x0301030005' in self.attrs.get('StoredVehicleDataResponseParsed'):
            if 'value' in self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030005']:
                value = self.attrs.get('StoredVehicleDataResponseParsed')['0x0301030005'].get('value', 0)
        return int(value)

    @property
    def is_combined_range_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301030005' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
        return False

    @property
    def fuel_level(self):
        value = -1
        if '0x030103000A' in self.attrs.get('StoredVehicleDataResponseParsed'):
            if 'value' in self.attrs.get('StoredVehicleDataResponseParsed')['0x030103000A']:
                value = self.attrs.get('StoredVehicleDataResponseParsed')['0x030103000A'].get('value', 0)
        return int(value)

    @property
    def is_fuel_level_supported(self):
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x030103000A' in self.attrs.get('StoredVehicleDataResponseParsed'):
                return True
        return False

  # Climatisation settings
    @property
    def climatisation_target_temperature(self):
        """Return the target temperature from climater."""
        if self.attrs.get('climater', False):
            value = self.attrs.get('climater').get('settings', {}).get('targetTemperature', {}).get('content', 2730)
        if value:
            reply = float((value / 10) - 273)
            return reply

    @property
    def is_climatisation_target_temperature_supported(self):
        """Return true if climatisation target temperature is supported."""
        if self.attrs.get('climater', False):
            if 'settings' in self.attrs.get('climater', {}):
                if 'targetTemperature' in self.attrs.get('climater', {})['settings']:
                    return True
        return False

    @property
    def climatisation_time_left(self):
        """Return time left for climatisation in hours:minutes."""
        if self.attrs.get('airConditioning', {}).get('remainingTimeToReachTargetTemperatureInSeconds', False):
            minutes = int(self.attrs.get('airConditioning', {}).get('remainingTimeToReachTargetTemperatureInSeconds', 0))/60
            try:
                if not 0 <= minutes <= 65535:
                    return "00:00"
                return "%02d:%02d" % divmod(minutes, 60)
            except Exception:
                pass
        return "00:00"

    @property
    def is_climatisation_time_left_supported(self):
        """Return true if remainingTimeToReachTargetTemperatureInSeconds is supported."""
        if self.attrs.get('airConditioning', {}).get('remainingTimeToReachTargetTemperatureInSeconds', False):
            return True
        return False

    @property
    def climatisation_without_external_power(self):
        """Return state of climatisation from battery power."""
        return self.attrs.get('climater').get('settings').get('climatisationWithoutHVpower').get('content', False)

    @property
    def is_climatisation_without_external_power_supported(self):
        """Return true if climatisation on battery power is supported."""
        if self.attrs.get('climater', False):
            if 'settings' in self.attrs.get('climater', {}):
                if 'climatisationWithoutHVpower' in self.attrs.get('climater', {})['settings']:
                    return True
            else:
                return False

    @property
    def outside_temperature(self):
        """Return outside temperature."""
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301020001'].get('value', 0))
        if response:
            return round(float((response / 10) - 273.15), 1)
        else:
            return False

    @property
    def is_outside_temperature_supported(self):
        """Return true if outside temp is supported"""
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301020001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                if "value" in self.attrs.get('StoredVehicleDataResponseParsed')['0x0301020001']:
                    return True
                else:
                    return False
            else:
                return False

  # Climatisation, electric
    @property
    def electric_climatisation(self):
        """Return status of climatisation."""
        if self.attrs.get('climater', {}).get('status', {}).get('climatisationStatusData', {}).get('climatisationState', {}).get('content', False):
            climatisation_type = self.attrs.get('climater', {}).get('settings', {}).get('heaterSource', {}).get('content', '')
            status = self.attrs.get('climater', {}).get('status', {}).get('climatisationStatusData', {}).get('climatisationState', {}).get('content', '')
            if status in ['heating', 'cooling', 'on'] and climatisation_type == 'electric':
                return True
        return False

    @property
    def is_electric_climatisation_supported(self):
        """Return true if vehichle has climater."""
        return self.is_climatisation_supported

    @property
    def auxiliary_climatisation(self):
        """Return status of auxiliary climatisation."""
        climatisation_type = self.attrs.get('climater', {}).get('settings', {}).get('heaterSource', {}).get('content', '')
        status = self.attrs.get('climater', {}).get('status', {}).get('climatisationStatusData', {}).get('climatisationState', {}).get('content', '')
        if status in ['heating', 'cooling', 'ventilation', 'heatingAuxiliary', 'on'] and climatisation_type == 'auxiliary':
            return True
        elif status in ['heatingAuxiliary'] and climatisation_type == 'electric':
            return True
        else:
            return False

    @property
    def is_auxiliary_climatisation_supported(self):
        """Return true if vehicle has auxiliary climatisation."""
        if self._services.get('rclima_v1', False):
            functions = self._services.get('rclima_v1', {}).get('operations', [])
            #for operation in functions:
            #    if operation['id'] == 'P_START_CLIMA_AU':
            if 'P_START_CLIMA_AU' in functions:
                    return True
        return False

    @property
    def is_climatisation_supported(self):
        """Return true if climatisation has State."""
        if self.attrs.get('climater', {}).get('status', {}).get('climatisationStatusData', {}).get('climatisationState', {}).get('content', False):
            return True
        return False

    @property
    def window_heater(self):
        """Return status of window heater."""
        if self.attrs.get('climater', False):
            status_front = self.attrs.get('climater', {}).get('status', {}).get('windowHeatingStatusData', {}).get('windowHeatingStateFront', {}).get('content', '')
            if status_front == 'on':
                return True
            status_rear = self.attrs.get('climater', {}).get('status', {}).get('windowHeatingStatusData', {}).get('windowHeatingStateRear', {}).get('content', '')
            if status_rear == 'on':
                return True
        return False

    @property
    def is_window_heater_supported(self):
        """Return true if vehichle has heater."""
        if self.is_electric_climatisation_supported:
            if self.attrs.get('climater', False):
                if self.attrs.get('climater', {}).get('status', {}).get('windowHeatingStatusData', {}).get('windowHeatingStateFront', {}).get('content', '') in ['on', 'off']:
                    return True
                if self.attrs.get('climater', {}).get('status', {}).get('windowHeatingStatusData', {}).get('windowHeatingStateRear', {}).get('content', '') in ['on', 'off']:
                    return True
        return False

    @property
    def seat_heating(self):
        """Return status of seat heating."""
        if self.attrs.get('airConditioning', {}).get('seatHeatingSupport', False):
            for element in self.attrs.get('airConditioning', {}).get('seatHeatingSupport', {}):
                if self.attrs.get('airConditioning', {}).get('seatHeatingSupport', {}).get(element, False):
                    return True
        return False

    @property
    def is_seat_heating_supported(self):
        """Return true if vehichle has seat heating."""
        if self.attrs.get('airConditioning', {}).get('seatHeatingSupport', False):
            return True
        return False

  # Parking heater, "legacy" auxiliary climatisation
    @property
    def pheater_duration(self):
        return self._climate_duration

    @pheater_duration.setter
    def pheater_duration(self, value):
        if value in [10, 20, 30, 40, 50, 60]:
            self._climate_duration = value
        else:
            _LOGGER.warning(f'Invalid value for duration: {value}')

    @property
    def is_pheater_duration_supported(self):
        return self.is_pheater_heating_supported

    @property
    def pheater_ventilation(self):
        """Return status of combustion climatisation."""
        return self.attrs.get('heating', {}).get('climatisationStateReport', {}).get('climatisationState', False) == 'ventilation'

    @property
    def is_pheater_ventilation_supported(self):
        """Return true if vehichle has combustion climatisation."""
        return self.is_pheater_heating_supported

    @property
    def pheater_heating(self):
        """Return status of combustion engine heating."""
        return self.attrs.get('heating', {}).get('climatisationStateReport', {}).get('climatisationState', False) == 'heating'

    @property
    def is_pheater_heating_supported(self):
        """Return true if vehichle has combustion engine heating."""
        if self.attrs.get('heating', {}).get('climatisationStateReport', {}).get('climatisationState', False):
            return True

    @property
    def pheater_status(self):
        """Return status of combustion engine heating/ventilation."""
        return self.attrs.get('heating', {}).get('climatisationStateReport', {}).get('climatisationState', 'Unknown')

    @property
    def is_pheater_status_supported(self):
        """Return true if vehichle has combustion engine heating/ventilation."""
        if self.attrs.get('heating', {}).get('climatisationStateReport', {}).get('climatisationState', False):
            return True

  # Windows
    @property
    def windows_closed(self):
        return (self.window_closed_left_front and self.window_closed_left_back and self.window_closed_right_front and self.window_closed_right_back)

    @property
    def is_windows_closed_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301050001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050001'].get('value', 0))
        return True if response != 0 else False

    @property
    def window_closed_left_front(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050001'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_left_front_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301050001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050001'].get('value', 0))
        return True if response != 0 else False

    @property
    def window_closed_right_front(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050005'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_right_front_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301050005' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050005'].get('value', 0))
        return True if response != 0 else False

    @property
    def window_closed_left_back(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050003'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_left_back_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301050003' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050003'].get('value', 0))
        return True if response != 0 else False

    @property
    def window_closed_right_back(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050007'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_right_back_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301050007' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301050007'].get('value', 0))
        return True if response != 0 else False

    @property
    def sunroof_closed(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030105000B'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_sunroof_closed_supported(self):
        """Return true if sunroof state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x030105000B' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030105000B'].get('value', 0))
        return True if response != 0 else False

  # Locks
    @property
    def door_locked(self):
        # LEFT FRONT
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040001'].get('value', 0))
        if response != 2:
            return False
        # LEFT REAR
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040004'].get('value', 0))
        if response != 2:
            return False
        # RIGHT FRONT
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040007'].get('value', 0))
        if response != 2:
            return False
        # RIGHT REAR
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000A'].get('value', 0))
        if response != 2:
            return False

        return True

    @property
    def is_door_locked_supported(self):
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301040001' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040001'].get('value', 0))
        return True if response != 0 else False

    @property
    def trunk_locked(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000D'].get('value', 0))
        if response == 2:
            return True
        else:
            return False

    @property
    def is_trunk_locked_supported(self):
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x030104000D' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000D'].get('value', 0))
        return True if response != 0 else False

  # Doors, hood and trunk
    @property
    def hood_closed(self):
        """Return true if hood is closed"""
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040011'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_hood_closed_supported(self):
        """Return true if hood state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301040011' in self.attrs.get('StoredVehicleDataResponseParsed', {}):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040011'].get('value', 0))
        return True if response != 0 else False

    @property
    def door_closed_left_front(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040002'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_left_front_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301040002' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040002'].get('value', 0))
        return True if response != 0 else False

    @property
    def door_closed_right_front(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040008'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_right_front_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301040008' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040008'].get('value', 0))
        return True if response != 0 else False

    @property
    def door_closed_left_back(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040005'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_left_back_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x0301040005' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x0301040005'].get('value', 0))
        return True if response != 0 else False

    @property
    def door_closed_right_back(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000B'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_right_back_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x030104000B' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000B'].get('value', 0))
        return True if response != 0 else False

    @property
    def trunk_closed(self):
        response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000E'].get('value', 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_trunk_closed_supported(self):
        """Return true if window state is supported"""
        response = 0
        if self.attrs.get('StoredVehicleDataResponseParsed', False):
            if '0x030104000E' in self.attrs.get('StoredVehicleDataResponseParsed'):
                response = int(self.attrs.get('StoredVehicleDataResponseParsed')['0x030104000E'].get('value', 0))
        return True if response != 0 else False

  # Departure timers
   # Under development
    @property
    def departure1(self):
        """Return timer status and attributes."""
        if self.attrs.get('departuretimer', False):
            try:
                data = {}
                timerdata = self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])
                profiledata = self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerProfileList', {}).get('timerProfile', [])
                timer = timerdata[0]
                profile = profiledata[0]
                timer.pop('timestamp', None)
                timer.pop('timerID', None)
                timer.pop('profileID', None)
                profile.pop('timestamp', None)
                profile.pop('profileName', None)
                profile.pop('profileID', None)
                data.update(timer)
                data.update(profile)
                return data
            except:
                pass
        elif self.attrs.get('timers', False):
            try:
                response = self.attrs.get('timers', [])
                if len(self.attrs.get('timers', [])) >= 1:
                    timer = response[0]
                    timer.pop('id', None)
                else:
                    timer = {}
                return timer
            except:
                pass
        return None

    @property
    def is_departure1_supported(self):
        """Return true if timer 1 is supported."""
        if len(self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])) >=1:
            return True
        elif len(self.attrs.get('timers', [])) >= 1:
            return True
        return False

    @property
    def departure2(self):
        """Return timer status and attributes."""
        if self.attrs.get('departuretimer', False):
            try:
                data = {}
                timerdata = self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])
                profiledata = self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerProfileList', {}).get('timerProfile', [])
                timer = timerdata[1]
                profile = profiledata[1]
                timer.pop('timestamp', None)
                timer.pop('timerID', None)
                timer.pop('profileID', None)
                profile.pop('timestamp', None)
                profile.pop('profileName', None)
                profile.pop('profileID', None)
                data.update(timer)
                data.update(profile)
                return data
            except:
                pass
        elif self.attrs.get('timers', False):
            try:
                response = self.attrs.get('timers', [])
                if len(self.attrs.get('timers', [])) >= 2:
                    timer = response[1]
                    timer.pop('id', None)
                else:
                    timer = {}
                return timer
            except:
                pass
        return None

    @property
    def is_departure2_supported(self):
        """Return true if timer 2 is supported."""
        if len(self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])) >= 2:
            return True
        elif len(self.attrs.get('timers', [])) >= 2:
            return True
        return False

    @property
    def departure3(self):
        """Return timer status and attributes."""
        if self.attrs.get('departuretimer', False):
            try:
                data = {}
                timerdata = self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])
                profiledata = self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerProfileList', {}).get('timerProfile', [])
                timer = timerdata[2]
                profile = profiledata[2]
                timer.pop('timestamp', None)
                timer.pop('timerID', None)
                timer.pop('profileID', None)
                profile.pop('timestamp', None)
                profile.pop('profileName', None)
                profile.pop('profileID', None)
                data.update(timer)
                data.update(profile)
                return data
            except:
                pass
        elif self.attrs.get('timers', False):
            try:
                response = self.attrs.get('timers', [])
                if len(self.attrs.get('timers', [])) >= 3:
                    timer = response[2]
                    timer.pop('id', None)
                else:
                    timer = {}
                return timer
            except:
                pass
        return None

    @property
    def is_departure3_supported(self):
        """Return true if timer 3 is supported."""
        if len(self.attrs.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])) >= 3:
            return True
        elif len(self.attrs.get('timers', [])) >= 3:
            return True
        return False

  # Trip data
    @property
    def trip_last_entry(self):
        return self.attrs.get('tripstatistics', {})

    @property
    def trip_last_average_speed(self):
        return self.trip_last_entry.get('averageSpeed')

    @property
    def is_trip_last_average_speed_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('averageSpeed', None)) in (float, int):
            return True

    @property
    def trip_last_average_electric_consumption(self):
        value = self.trip_last_entry.get('averageElectricEngineConsumption')
        return float(value/10)

    @property
    def is_trip_last_average_electric_consumption_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('averageElectricEngineConsumption', None)) in (float, int):
            return True

    @property
    def trip_last_average_fuel_consumption(self):
        return int(self.trip_last_entry.get('averageFuelConsumption')) / 10

    @property
    def is_trip_last_average_fuel_consumption_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('averageFuelConsumption', None)) in (float, int):
            return True

    @property
    def trip_last_average_auxillary_consumption(self):
        return self.trip_last_entry.get('averageAuxiliaryConsumption')

    @property
    def is_trip_last_average_auxillary_consumption_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('averageAuxiliaryConsumption', None)) in (float, int):
            return True

    @property
    def trip_last_average_aux_consumer_consumption(self):
        value = self.trip_last_entry.get('averageAuxConsumerConsumption')
        return float(value / 10)

    @property
    def is_trip_last_average_aux_consumer_consumption_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('averageAuxConsumerConsumption', None)) in (float, int):
            return True

    @property
    def trip_last_duration(self):
        return self.trip_last_entry.get('traveltime')

    @property
    def is_trip_last_duration_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('traveltime', None)) in (float, int):
            return True

    @property
    def trip_last_length(self):
        return self.trip_last_entry.get('mileage')

    @property
    def is_trip_last_length_supported(self):
        response = self.trip_last_entry
        if response and type(response.get('mileage', None)) in (float, int):
            return True

    @property
    def trip_last_recuperation(self):
        #Not implemented
        return self.trip_last_entry.get('recuperation')

    @property
    def is_trip_last_recuperation_supported(self):
        #Not implemented
        response = self.trip_last_entry
        if response and type(response.get('recuperation', None)) in (float, int):
            return True

    @property
    def trip_last_average_recuperation(self):
        #Not implemented
        value = self.trip_last_entry.get('averageRecuperation')
        return float(value / 10)

    @property
    def is_trip_last_average_recuperation_supported(self):
        #Not implemented
        response = self.trip_last_entry
        if response and type(response.get('averageRecuperation', None)) in (float, int):
            return True

    @property
    def trip_last_total_electric_consumption(self):
        #Not implemented
        return self.trip_last_entry.get('totalElectricConsumption')

    @property
    def is_trip_last_total_electric_consumption_supported(self):
        #Not implemented
        response = self.trip_last_entry
        if response and type(response.get('totalElectricConsumption', None)) in (float, int):
            return True

  # Status of set data requests
    @property
    def refresh_action_status(self):
        """Return latest status of data refresh request."""
        return self._requests.get('refresh', {}).get('status', 'None')

    @property
    def charger_action_status(self):
        """Return latest status of charger request."""
        return self._requests.get('batterycharge', {}).get('status', 'None')

    @property
    def climater_action_status(self):
        """Return latest status of climater request."""
        return self._requests.get('climatisation', {}).get('status', 'None')

    @property
    def pheater_action_status(self):
        """Return latest status of parking heater request."""
        return self._requests.get('preheater', {}).get('status', 'None')

    @property
    def honkandflash_action_status(self):
        """Return latest status of honk and flash action request."""
        return self._requests.get('honkandflash', {}).get('status', 'None')

    @property
    def lock_action_status(self):
        """Return latest status of lock action request."""
        return self._requests.get('lock', {}).get('status', 'None')

    @property
    def timer_action_status(self):
        """Return latest status of lock action request."""
        return self._requests.get('departuretimer', {}).get('status', 'None')

    @property
    def refresh_data(self):
        """Get state of data refresh"""
        if self._requests.get('refresh', {}).get('id', False):
            return True

    @property
    def is_refresh_data_supported(self):
        """Data refresh is supported."""
        if 'ONLINE' in self._connectivities:
            return True

   # Honk and flash
    @property
    def request_honkandflash(self):
        """State is always False"""
        return False

    @property
    def is_request_honkandflash_supported(self):
        """Honk and flash is supported if service is enabled."""
        if self._services.get('rhonk_v1', False):
            return True

    @property
    def request_flash(self):
        """State is always False"""
        return False

    @property
    def is_request_flash_supported(self):
        """Honk and flash is supported if service is enabled."""
        if self._services.get('rhonk_v1', False):
            return True

  # Requests data
    @property
    def refresh_data(self):
        """Get state of data refresh"""
        if self._requests.get('refresh', {}).get('id', False):
            return True

    @property
    def is_refresh_data_supported(self):
        """Data refresh is always supported."""
        return True

    @property
    def request_in_progress(self):
        """Request in progress is always supported."""
        try:
            for section in self._requests:
                if self._requests[section].get('id', False):
                    return True
        except:
            pass
        return False

    @property
    def is_request_in_progress_supported(self):
        """Request in progress is always supported."""
        return True

    @property
    def request_results(self):
        """Get last request result."""
        data = {
            'latest': self._requests.get('latest', None),
            'state': self._requests.get('state', None)
        }
        for section in self._requests:
            if section in ['departuretimer', 'batterycharge', 'climatisation', 'refresh', 'lock', 'preheater']:
                data[section] = self._requests[section].get('status', 'Unknown')
        return data

    @property
    def is_request_results_supported(self):
        """Request results is supported if in progress is supported."""
        return self.is_request_in_progress_supported

    @property
    def requests_remaining(self):
        """Get remaining requests before throttled."""
        if self.attrs.get('rate_limit_remaining', False):
            self.requests_remaining = self.attrs.get('rate_limit_remaining')
            self.attrs.pop('rate_limit_remaining')
        return self._requests['remaining']

    @requests_remaining.setter
    def requests_remaining(self, value):
        self._requests['remaining'] = value

    @property
    def is_requests_remaining_supported(self):
        if self.is_request_in_progress_supported:
            return True if self._requests.get('remaining', False) else False

 #### Helper functions ####
    def __str__(self):
        return self.vin

    @property
    def json(self):
        def serialize(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()

        return to_json(
            OrderedDict(sorted(self.attrs.items())),
            indent=4,
            default=serialize
        )
