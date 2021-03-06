import argparse
import json
import os
import re
import struct
import time
from datetime import datetime

import requests
from geopy.geocoders import GoogleV3
from google.protobuf.internal import encoder
from gpsoauth import perform_master_login, perform_oauth
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from s2sphere import *

import pokemon_pb2

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def encode(cellid):
    output = []
    encoder._VarintEncoder()(output.append, cellid)
    return ''.join(output)


def get_neighbors():
    origin = CellId.from_lat_lng(LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)).parent(15)
    walk = [origin.id()]
    # 10 before and 10 after
    next = origin.next()
    prev = origin.prev()
    for i in range(10):
        walk.append(prev.id())
        walk.append(next.id())
        next = next.next()
        prev = prev.prev()
    return walk


API_URL = 'https://pgorelease.nianticlabs.com/plfe/rpc'
LOGIN_URL = "https://sso.pokemon.com/sso/login?" \
            "service=https%3A%2F%2Fsso.pokemon.com%2Fsso%2Foauth2.0%2FcallbackAuthorize"
LOGIN_OAUTH = 'https://sso.pokemon.com/sso/oauth2.0/accessToken'

ANDROID_ID = '9774d56d682e549c'
SERVICE = 'audience:server:client_id:848232511240-7so421jotr2609rmqakceuu1luuq0ptb.apps.googleusercontent.com'
APP = 'com.nianticlabs.pokemongo'
CLIENT_SIG = '321187995bc7cdc2b5fc91b11a96e2baa8602c62'

SESSION = requests.session()
SESSION.headers.update({'User-Agent': 'Niantic App'})
SESSION.verify = False

DEBUG = False
COORDS_LATITUDE = 0
COORDS_LONGITUDE = 0
COORDS_ALTITUDE = 0
FLOAT_LAT = 0
FLOAT_LONG = 0
deflat, deflng = 0, 0
default_step = 0.001

CONFIG = "config.json"
NUM_STEPS = 5
DATA_FILE = 'data.json'
DATA = []

HEARTBEATSTEP = 0
API_ENDPOINT = ''
ACCESS_TOKEN = ''
RESPONSE = ''
ORIGIN = ''


def f2i(float):
    return struct.unpack('<Q', struct.pack('<d', float))[0]


def f2h(float):
    return hex(struct.unpack('<Q', struct.pack('<d', float))[0])


def h2f(hex):
    return struct.unpack('<d', struct.pack('<Q', int(hex, 16)))[0]


def prune():
    # prune despawned pokemon
    cur_time = int(time.time())
    for i, poke in reversed(list(enumerate(DATA))):
        if poke['type'].lower() == 'pokemon':
            poke['timeleft'] = poke['timeleft'] - (cur_time - poke['timestamp'])
            poke['timestamp'] = cur_time
            if poke['timeleft'] <= 0:
                DATA.pop(i)


def write_data_to_file():
    prune()

    with open(DATA_FILE, 'w') as f:
        json.dump(DATA, f, indent=2)


def add_pokemon(pokeId, name, lat, lng, timestamp, timeleft):
    new_poke = True

    for data_cell in DATA:
        if data_cell['lat'] == lat and data_cell['lng'] == lng and data_cell['id'] == pokeId \
                and data_cell['type'] == 'pokemon':
            new_poke = False
            break

    if new_poke:
        DATA.append({
            'id': pokeId,
            'name': name,
            'lat': lat,
            'lng': lng,
            'timestamp': timestamp,
            'timeleft': timeleft,
            'type': 'pokemon'
        })


def add_pokestop(lat, lng):
    new_stop = True

    for data_cell in DATA:
        if data_cell['lat'] == lat and data_cell['lng'] == lng and data_cell['type'] == 'pokestop':
            new_stop = False
            break

    if new_stop:
        DATA.append({
            'id': 'Pokestop',
            'lat': lat,
            'lng': lng,
            'type': 'pokestop'
        })


def set_location(location_name):
    geolocator = GoogleV3()
    loc = geolocator.geocode(location_name)

    print('[!] Your given location: {}'.format(loc.address.encode('utf-8')))
    print('[!] lat/long/alt: {} {} {}'.format(loc.latitude, loc.longitude, loc.altitude))

    global deflat
    global deflng
    deflat, deflng = loc.latitude, loc.longitude

    set_location_coords(loc.latitude, loc.longitude, loc.altitude)


def set_location_coords(lat, long, alt):
    global COORDS_LATITUDE, COORDS_LONGITUDE, COORDS_ALTITUDE
    global FLOAT_LAT, FLOAT_LONG
    FLOAT_LAT = lat
    FLOAT_LONG = long
    COORDS_LATITUDE = f2i(lat)  # 0x4042bd7c00000000 # f2i(lat)
    COORDS_LONGITUDE = f2i(long)  # 0xc05e8aae40000000 #f2i(long)
    COORDS_ALTITUDE = f2i(alt)


def get_location_coords():
    return COORDS_LATITUDE, COORDS_LONGITUDE, COORDS_ALTITUDE


def api_req(service, api_endpoint, access_token, *mehs, **kw):
    while True:
        try:
            p_req = pokemon_pb2.RequestEnvelop()
            p_req.rpc_id = 2508056722472460033

            p_req.unknown1 = 2

            p_req.latitude, p_req.longitude, p_req.altitude = get_location_coords()

            p_req.unknown12 = 989

            if 'useauth' not in kw or not kw['useauth']:
                p_req.auth.provider = service
                p_req.auth.token.contents = access_token
                p_req.auth.token.unknown13 = 14
            else:
                p_req.unknown11.unknown71 = kw['useauth'].unknown71
                p_req.unknown11.unknown72 = kw['useauth'].unknown72
                p_req.unknown11.unknown73 = kw['useauth'].unknown73

            for meh in mehs:
                p_req.MergeFrom(meh)

            protobuf = p_req.SerializeToString()

            r = SESSION.post(api_endpoint, data=protobuf, verify=False)

            p_ret = pokemon_pb2.ResponseEnvelop()
            p_ret.ParseFromString(r.content)

            if DEBUG:
                print("REQUEST:")
                print(p_req)
                print("Response:")
                print(p_ret)
                print("\n\n")

            if DEBUG:
                print("[ ] Sleeping for 1 second")
            time.sleep(1)
            return p_ret
        except Exception, e:
            if DEBUG:
                print(e)
            print('[-] API request error, retrying')
            time.sleep(3)
            continue


def get_profile(service, access_token, api, useauth, *reqq):
    req = pokemon_pb2.RequestEnvelop()

    req1 = req.requests.add()
    req1.type = 2
    if len(reqq) >= 1:
        req1.MergeFrom(reqq[0])

    req2 = req.requests.add()
    req2.type = 126
    if len(reqq) >= 2:
        req2.MergeFrom(reqq[1])

    req3 = req.requests.add()
    req3.type = 4
    if len(reqq) >= 3:
        req3.MergeFrom(reqq[2])

    req4 = req.requests.add()
    req4.type = 129
    if len(reqq) >= 4:
        req4.MergeFrom(reqq[3])

    req5 = req.requests.add()
    req5.type = 5
    if len(reqq) >= 5:
        req5.MergeFrom(reqq[4])

    return api_req(service, api, access_token, req, useauth=useauth)


def get_api_endpoint(service, access_token, api=API_URL):
    p_ret = get_profile(service, access_token, api, None)
    try:
        return 'https://%s/rpc' % p_ret.api_url
    except:
        return None


def login_google(username, password):
    print('[!] Google login for: {}'.format(username))
    r1 = perform_master_login(username, password, ANDROID_ID)
    r2 = perform_oauth(username, r1.get('Token', ''), ANDROID_ID, SERVICE, APP, CLIENT_SIG)

    return r2.get('Auth')


def login_ptc(username, password):
    print('[!] login for: {}'.format(username))
    head = {'User-Agent': 'niantic'}
    r = SESSION.get(LOGIN_URL, headers=head)
    jdata = json.loads(r.content)
    data = {
        'lt': jdata['lt'],
        'execution': jdata['execution'],
        '_eventId': 'submit',
        'username': username,
        'password': password,
    }
    r1 = SESSION.post(LOGIN_URL, data=data, headers=head)

    ticket = None
    try:
        ticket = re.sub('.*ticket=', '', r1.history[0].headers['Location'])
    except Exception, e:
        if DEBUG:
            print(r1.json()['errors'][0])
        return None

    data1 = {
        'client_id': 'mobile-app_pokemon-go',
        'redirect_uri': 'https://www.nianticlabs.com/pokemongo/error',
        'client_secret': 'w8ScCUXJQc6kXKw8FiOhd8Fixzht18Dq3PEVkUCP5ZPxtgyWsbTvWHFLm2wNY0JR',
        'grant_type': 'refresh_token',
        'code': ticket,
    }

    r2 = SESSION.post(LOGIN_OAUTH, data=data1)
    access_token = re.sub('&expires.*', '', r2.content)
    access_token = re.sub('.*access_token=', '', access_token)
    return access_token


def raw_heartbeat(service, api_endpoint, access_token, response):
    m4 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleInt()
    m.f1 = int(time.time() * 1000)
    m4.message = m.SerializeToString()
    m5 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleString()
    m.bytes = "05daf51635c82611d1aac95c0b051d3ec088a930"
    m5.message = m.SerializeToString()

    walk = sorted(get_neighbors())

    m1 = pokemon_pb2.RequestEnvelop.Requests()
    m1.type = 106
    m = pokemon_pb2.RequestEnvelop.MessageQuad()
    m.f1 = ''.join(map(encode, walk))
    m.f2 = "\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000"
    m.lat = COORDS_LATITUDE
    m.long = COORDS_LONGITUDE
    m1.message = m.SerializeToString()
    response = get_profile(service, access_token, api_endpoint, response.unknown7, m1,
                           pokemon_pb2.RequestEnvelop.Requests(), m4, pokemon_pb2.RequestEnvelop.Requests(),  m5)
    payload = response.payload[0]
    heartbeat = pokemon_pb2.ResponseEnvelop.HeartbeatPayload()
    heartbeat.ParseFromString(payload)
    return heartbeat


def heartbeat(service, api_endpoint, access_token, response):
    while True:
        try:
            h = raw_heartbeat(service, api_endpoint, access_token, response)
            return h
        except Exception, e:
            if DEBUG:
                print(e)
            print('[-] Heartbeat missed, retrying')

            global HEARTBEATSTEP
            HEARTBEATSTEP += 1

            if HEARTBEATSTEP >= 5:
                return None


def scan(service, api_endpoint, access_token, response, origin, pokemons):
    steps = 0
    steplimit = NUM_STEPS
    pos = 1
    x = 0
    y = 0
    dx = 0
    dy = -1
    while steps < steplimit ** 2:
        original_lat = FLOAT_LAT
        original_long = FLOAT_LONG
        parent = CellId.from_lat_lng(LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)).parent(15)

        h = heartbeat(service, api_endpoint, access_token, response)
        
        if h is None:
            break
        
        hs = [h]
        for child in parent.children():
            latlng = LatLng.from_point(Cell(child).get_center())
            set_location_coords(latlng.lat().degrees, latlng.lng().degrees, 0)
            hs.append(heartbeat(service, api_endpoint, access_token, response))
        set_location_coords(original_lat, original_long, 0)

        visible_pokemons = []

        for hh in hs:
            for cell in hh.cells:
                for wild in cell.WildPokemon:
                    visible_pokemons.append(wild)
                if cell.Fort:
                    for Fort in cell.Fort:
                        if Fort.Enabled:
                            # if Fort.GymPoints:
                            #     gyms.append([Fort.Team, Fort.Latitude, Fort.Longitude])
                            if Fort.FortType == 1:
                                add_pokestop(Fort.Latitude, Fort.Longitude)

        for cell in h.cells:
            if cell.NearbyPokemon:
                other = LatLng.from_point(Cell(CellId(cell.S2CellId)).get_center())
                diff = other - origin
                # print(diff)
                difflat = diff.lat().degrees
                difflng = diff.lng().degrees
                if len(cell.NearbyPokemon) > 0:
                    print('[+] Found pokemon!')
                for poke in cell.NearbyPokemon:
                    print('    (%s) %s' % (poke.PokedexNumber, pokemons[poke.PokedexNumber - 1]['Name']))

        for poke in visible_pokemons:
            other = LatLng.from_degrees(poke.Latitude, poke.Longitude)
            diff = other - origin
            # print(diff)
            difflat = diff.lat().degrees
            difflng = diff.lng().degrees

            timestamp = int(time.time())
            add_pokemon(poke.pokemon.PokemonId, pokemons[poke.pokemon.PokemonId - 1]['Name'], poke.Latitude,
                        poke.Longitude, timestamp, poke.TimeTillHiddenMs / 1000)

        write_data_to_file()

        if (-steplimit / 2 < x <= steplimit / 2) and (-steplimit / 2 < y <= steplimit / 2):
            set_location_coords((x * 0.0025) + deflat, (y * 0.0025) + deflng, 0)
        if x == y or (x < 0 and x == -y) or (x > 0 and x == 1 - y):
            dx, dy = -dy, dx
        x, y = x + dx, y + dy
        steps += 1

        print('[+] Scan: %0.1f %%' % (((steps + (pos * .25) - .25) / steplimit ** 2) * 100))


def main():
    write_data_to_file()
    pokemons = json.load(open('pokemon.json'))

    load = {}
    if os.path.isfile(CONFIG):
        with open(CONFIG) as data:
            load.update(json.load(data))

    # Read passed in Arguments
    required = lambda x: not x in load

    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--auth_service", help="Auth Service", required=required("auth_service"))
    parser.add_argument("-u", "--username", help="PTC Username", required=required("username"))
    parser.add_argument("-p", "--password", help="PTC Password", required=required("password"))
    parser.add_argument("-l", "--location", help="Location", required=required("location"))
    parser.add_argument("-s", "--step", help="Steps")
    parser.add_argument("-d", "--debug", help="Debug Mode", action='store_true')
    parser.set_defaults(DEBUG=False)
    args = parser.parse_args()

    for key in args.__dict__:
        if key in load and args.__dict__[key] == None:
            args.__dict__[key] = load[key]

    if args.debug:
        global DEBUG
        DEBUG = True
        print('[!] DEBUG mode on')

    if args.step:
        global NUM_STEPS
        try:
            NUM_STEPS = int(args.step)
        except ValueError:
            print('[!] Invalid amount of steps, this needs to be a number')
            return
        print('[!] Amount of steps {}'.format(NUM_STEPS))

    set_location(args.location)

    if args.auth_service == 'ptc':
        access_token = login_ptc(args.username, args.password)
    else:
        access_token = login_google(args.username, args.password)

    if access_token is None:
        print('[-] Error logging in: possible wrong username/password')
        return
    print('[+] RPC Session Token: {} ...'.format(access_token))

    api_endpoint = get_api_endpoint(args.auth_service, access_token)
    if api_endpoint is None:
        print('[-] RPC server offline')
        return
    print('[+] Received API endpoint: {}'.format(api_endpoint))

    response = get_profile(args.auth_service, access_token, api_endpoint, None)
    if response is not None:
        print('[+] Login successful')

        payload = response.payload[0]
        profile = pokemon_pb2.ResponseEnvelop.ProfilePayload()
        profile.ParseFromString(payload)
        print('[+] Username: {}'.format(profile.profile.username))

        creation_time = datetime.fromtimestamp(int(profile.profile.creation_time) / 1000)
        print('[+] You are playing Pokemon Go since: {}'.format(
            creation_time.strftime('%Y-%m-%d %H:%M:%S'),
        ))

        for curr in profile.profile.currency:
            print('[+] {}: {}'.format(curr.type, curr.amount))
    else:
        print('[-] Response problem')

    originloc = LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)

    global API_ENDPOINT, ACCESS_TOKEN, RESPONSE, ORIGIN
    API_ENDPOINT = api_endpoint
    ACCESS_TOKEN = access_token
    RESPONSE = response
    ORIGIN = originloc


    while True:
        try:
            global HEARTBEATSTEP
            if HEARTBEATSTEP >= 5:
                set_location(args.location)

                if args.auth_service == 'ptc':
                    access_token = login_ptc(args.username, args.password)
                else:
                    access_token = login_google(args.username, args.password)

                if access_token is None:
                    print('[-] Error logging in: possible wrong username/password')
                else:
                    print('[+] RPC Session Token: {} ...'.format(access_token))

                    api_endpoint = get_api_endpoint(args.auth_service, access_token)
                    if api_endpoint is None:
                        print('[-] RPC server offline')
                    else:
                        print('[+] Received API endpoint: {}'.format(api_endpoint))

                        response = get_profile(args.auth_service, access_token, api_endpoint, None)
                        if response is not None:
                            print('[+] Login successful')

                            payload = response.payload[0]
                            profile = pokemon_pb2.ResponseEnvelop.ProfilePayload()
                            profile.ParseFromString(payload)
                            print('[+] Username: {}'.format(profile.profile.username))

                            creation_time = datetime.fromtimestamp(int(profile.profile.creation_time) / 1000)
                            print('[+] You are playing Pokemon Go since: {}'.format(
                                creation_time.strftime('%Y-%m-%d %H:%M:%S'),
                            ))

                            for curr in profile.profile.currency:
                                print('[+] {}: {}'.format(curr.type, curr.amount))

                            originloc = LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)

                            API_ENDPOINT = api_endpoint
                            ACCESS_TOKEN = access_token
                            RESPONSE = response
                            ORIGIN = originloc

                            HEARTBEATSTEP = 0
                        else:
                            print('[-] Response problem')
            else:
                scan(args.auth_service, API_ENDPOINT, ACCESS_TOKEN, RESPONSE, ORIGIN, pokemons)
        except Exception, e:
            pass


if __name__ == '__main__':
    main()
