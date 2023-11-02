# play a WAV file on a Raspberry Pi Pico using CircuitPython
#
# State Machine based on https://learn.adafruit.com/circuitpython-101-state-machines/overview

import board
import busio
import audiocore
import audiobusio
import digitalio
import os
import pwmio
import random
import sdcardio
import storage
import supervisor
import time
import cptoml
from adafruit_debouncer import Debouncer

# Set to false to disable testing/tracing code
TESTING = True

CONFIG_FILE = "/sd/config.toml"
AMBIENT_FILE = "/sd/000.wav"
TRIGGER_FILE = "/sd/001.wav"

TRIGGER_DURATION = 5.0

# Pins
INPUT_PIN = board.GP17    # standard MiniSSO
OUTPUT1_PIN = board.GP16  # standard MiniSSO
OUTPUT2_PIN = board.GP13  # standard MiniSSO
I2S_BIT_CLOCK_PIN = board.GP26
I2S_WORD_SELECT_PIN = board.GP27
I2S_DATA_PIN = board.GP28
SPI_CLOCK_PIN = board.GP14
SPI_MOSI_PIN = board.GP15
SPI_MISO_PIN = board.GP8
SD_DETECT_PIN = board.GP7
SD_CS_PIN = board.GP11

NANOSECONDS_PER_SECOND = 1000000000
config = {}

# MiniSSO pins
trigger_io = digitalio.DigitalInOut(INPUT_PIN)
trigger_io.direction = digitalio.Direction.INPUT
trigger = Debouncer(trigger_io)

output1 = pwmio.PWMOut(OUTPUT1_PIN, frequency=1400)
output2 = pwmio.PWMOut(OUTPUT2_PIN, frequency=1400)

# MiniWAV

detect = digitalio.DigitalInOut(SD_DETECT_PIN)
detect.direction = digitalio.Direction.INPUT

#if not detect.value:
#    print("SD Card not detected")
#    while True:
#        pass

i2s = audiobusio.I2SOut(I2S_BIT_CLOCK_PIN, I2S_WORD_SELECT_PIN, I2S_DATA_PIN)

spi = busio.SPI(clock=SPI_CLOCK_PIN, MOSI=SPI_MOSI_PIN, MISO=SPI_MISO_PIN)

sdcard = sdcardio.SDCard(spi, SD_CS_PIN)
vfs = storage.VfsFat(sdcard)
storage.mount(vfs, "/sd")

ambient_audio_exists = False
try:
    status = os.stat(AMBIENT_FILE)
    ambient_audio_exists = True
except OSError:
    pass

trigger_audio_exists = False
try:
    status = os.stat(TRIGGER_FILE)
    trigger_audio_exists = True
except OSError as err:
    print('Required trigger audio not found:', err)
    while True:
        pass

time.sleep(3) # delay a bit for logging

print("Starting up...")

###########################################################
# Support functions

def log(s):
    """Print the argument if testing/tracing is enabled."""
    if TESTING:
        print(s)

def upscale(x):
    """Convert an 8 bit number to a 16 bit value.

    Used to convert 0-255 dimming values into 0-65535
    values used by Raspberry Pi Pico PWM.
    """
    return (x << 8) + x

############################################################
# Effects

class Effect(object):

    def __init__(self, pwmout):
        self.__pwmout = pwmout
        self.__dim = 0

    @property
    def dim(self):
        return self.__dim

    @dim.setter
    def dim(self, value):
        self.__dim = value

    def enter(self):
        pass

    def exit(self):
        pass

    def update(self, now=None):
        pass

class Dimmer(Effect):

    def __init__(self, pwmout, gamma=2.2):
        super().__init__(pwmout)
        self.__dim = 0
        self.__strobe = 0
        self.__last_strobe = self.__strobe
        self.transition_period = 0
        self.last_transition_time = 0
        self.next_transition = True  # on

        #
        # Create a gamma correction table.
        # Indexed from 0-255 for compatibility with DMX
        # Value from table will be 0-65535 for Pico
        # 'duty_cycle'.
        #
        # Source: https://www.teamten.com/lawrence/graphics/gamma/
        #
        # gamma table
        max_in = 255
        max_out = 65535
        self.__linear_to_gamma = []

        for i in range(max_in + 1):
            self.__linear_to_gamma.append(int(((float(i) / float(max_in)) ** gamma) * max_out + 0.5))

        # initialize to off
        self.__pwmout.duty_cycle = self.__dim
        self.__last_dim = self.__dim

    @property
    def dim(self):
        return self.__dim
        
    @dim.setter
    def dim(self, value):
        if (value < 0):
            value = 0
        if (value > 255):
            value = 255

        self.__dim = value

    @property
    def strobe(self):
        return self.__strobe

    @strobe.setter
    def strobe(self, frequency):
        if (frequency < 0):
            frequency = 0
        if (frequency > 1400):
            frequency = 1400

        self.__last_strobe = self.__strobe
        self.__strobe = frequency

        if (frequency > 0):
            # convert frequency into transition period
            # (period is equally divided to half off/half on)
            self.transition_period = int((NANOSECONDS_PER_SECOND / frequency) // 2)
            log('Strobe transition period: {0:d}'.format(self.transition_period))

    def enter(self):
        # set to turn on if strobing on next update()
        self.last_transition_time = 0
        self.next_transition = True

        self.__last_dim = None

    def exit(self):
        pass

    def update(self, now=None):
        if (self.__strobe == 0) :
            # constant
            if (self.__last_dim == None) or (self.__last_dim != self.__dim):
                self.__pwmout.duty_cycle = self.__linear_to_gamma[self.__dim]
                self.__last_dim = self.__dim
        else:
            # strobing
            if (now == None):
                now = time.monotonic_ns()

            if now >= (self.last_transition_time + self.transition_period):
                self.last_transition_time = now
                # transition between on/off
                if self.next_transition:
                    self.__pwmout.duty_cycle = self.__linear_to_gamma[self.__dim]
                    self.__last_dim = self.__dim
                else:
                    self.__pwmout.duty_cycle = 0
                    self.__last_dim = 0

                self.next_transition = not self.next_transition
    
#
# Candle flicker: https://github.com/cpldcpu/SimPad/blob/master/Toolchain/examples/candleflicker/candleflicker.c
#
class Flicker(Effect):

    # original seed 0x55ce
    def __init__(self, pwmout, seed):
        super().__init__(pwmout)
        self.rnd_lfsr = seed
        self.last_flicker = 0
        self.__duty_cycle = 0

    @property
    def duty_cycle(self):
        return self.__duty_cycle

    @property
    def dim(self):
        return self.__dim

    @dim.setter
    def dim(self, value):
        if (value < 0):
            value = 0

        if (value > 5):
            value = 5

        self.__dim = value

    def _lfsr_step(self, rnd_lfsr):
        if (rnd_lfsr & 1):
            return (rnd_lfsr >> 1) ^ 0x822B
        else:
            return rnd_lfsr >> 1

    def update(self, now=None):
        # close to 30Hz
        if (now == None):
            now = time.monotonic_ns()

        if now >= (self.last_flicker + 33333333):
            if (self.__dim == 0):
                # off
                self.__pwmout.duty_cycle = 0
                return 0

            self.last_flicker = now
            lowpass = self.__duty_cycle

            newval = 0
            if (self.rnd_lfsr & 0x100):
                newval = 255
            else:
                newval = self.rnd_lfsr & 255

            #self.__duty_cycle = newval << 8; # no filter
            self.__duty_cycle = lowpass - (lowpass >> 1) + (newval << 7) # IRR filter with lag 2 (recommened)
            #self.__duty_cycle = lowpass - (lowpass >> 2) + (newval << 6) # IRR filter with lag 4 (less flicker)
            #self.__duty_cycle = lowpass - (lowpass >> 3) + (newval << 5) # IRR filter with lag 2 (event less flicker)

            # dim duty_cycle
            # invert so that 1 is dimmest and 5 is brightest
            # 1 => 5
            # 2 => 4
            # 3 => 3
            # 4 => 2
            # 5 => 1
            self.__duty_cycle = self.__duty_cycle >> (6 - self.__dim)

            self.__pwmout.duty_cycle = self.__duty_cycle

            #log('Flicker value: {0:d}'.format(self.__duty_cycle))

            for i in range(3):
                self.rnd_lfsr = self._lfsr_step(self.rnd_lfsr)
                if ((self.rnd_lfsr & 0xff) > 128):
                    break

        return self.__duty_cycle

###########################################################
# State Machine

class StateMachine(object):

    def __init__(self):
        self.state = None
        self.states = {}
      
    def add_state(self, state):
        self.states[state.name] = state

    def go_to_state(self, state_name):
        if self.state:
            log('Exiting {0}'.format(self.state.name))
            self.state.exit(self)
        self.state = self.states[state_name]
        log('Entering {0}'.format(self.state.name))
        self.state.enter(self)

    def update(self):
        if self.state:
            #log('Updating {0}'.format(self.state.name))
            self.state.update(self)

###########################################################
# States

# Abstract parent state class

class State(object):

    def __init__(self):
        self.effects = []
        self.i2s = None
        self.wave = None

    @property
    def name(self):
        return ''

    def add_effect(self, effect):
        self.effects.append(effect)

    def set_i2s(self, value):
        self.i2s = value

    def set_wave(self, value):
        self.wave = value

    def enter(self, machine):
        pass

    def exit(self, machine):
        pass

    def update(self, machine):
        return True
    
class AmbientState(State):

    def __init__(self):
        super().__init__()

    @property
    def name(self):
        return 'ambient'

    def enter(self, machine):
        if self.wave != None:
            self.i2s.play(self.wave, loop=True)

        State.enter(self, machine)
        for effect in self.effects:
            effect.enter()

    def exit(self, machine):
        if self.wave != None:
            self.i2s.stop()

        State.exit(self, machine)
        for effect in self.effects:
            effect.exit()

    def update(self, machine):
        if State.update(self, machine):
            now = time.monotonic_ns()
            for effect in self.effects:
                effect.update(now=now)

        if trigger.fell:
            machine.go_to_state('triggered')

class TriggeredState(State):

    def __init__(self):
        super().__init__()
        self.trigger_finish_time = 0

    @property
    def name(self):
        return 'triggered'
    
    def enter(self, machine):
        self.i2s.play(self.wave, loop=False)

        State.enter(self, machine)
        for effect in self.effects:
            effect.enter()
        now = time.monotonic()
        self.trigger_finish_time = now + TRIGGER_DURATION

    def exit(self, machine):
        State.exit(self, machine)
        for effect in self.effects:
            effect.exit()

    def update(self, machine):
        # triggered length based on audio length
        if not self.i2s.playing:
            machine.go_to_state('ambient')
            return

        if State.update(self, machine):
            now = time.monotonic_ns()
            for effect in self.effects:
                effect.update(now=now)
        
        # timed trigger versus trigger based on triggered audio length
        #now = time.monotonic()
        #if now >= self.trigger_finish_time:
        #    machine.go_to_state('ambient')

###########################################################

# delete me
# linear dim up
#print('linear dim up')
#for x in range(256):
#    output2.duty_cycle = upscale(x)
#    time.sleep(.1)

# gamma dim up
#print('gamma dim up')
#for x in range(256):
#    output2.duty_cycle = linear_to_gamma[x]
#    time.sleep(.1)

#print('Maximum output')
#time.sleep(3)
#output2.duty_cycle = 0

#--------------

state_machine = StateMachine()

ambient_state = AmbientState()
if ambient_audio_exists:
    ambient_buffer = bytearray(1024)
    ambient_wav = audiocore.WaveFile(AMBIENT_FILE, ambient_buffer)
    ambient_state.set_i2s(i2s)
    ambient_state.set_wave(ambient_wav)

try:
    output1_config = {'effect': 'dimmer', 'dim': 0, 'strobe': 0, 'seed': 0x55ce}
    output2_config = {'effect': 'dimmer', 'dim': 0, 'strobe': 0, 'seed': 0x55ce}
    subtable = 'ambient'
    keys = cptoml.keys(subtable, toml=CONFIG_FILE)
    for key in keys:
        if key == 'effect1':
            output1_config['effect'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect1.dim':
            output1_config['dim'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect1.strobe':
            output1_config['strobe'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect1.seed':
            output1_config['seed'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2':
            output2_config['effect'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2.dim':
            output2_config['dim'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2.strobe':
            output2_config['strobe'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2.seed':
            output2_config['seed'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
except OSError as err:
    print("Application configuration not found:", err)

if output1_config['effect'] == 'dimmer':
    effect = Dimmer(output1)
    effect.dim = output1_config['dim']
    effect.strobe = output1_config['strobe']
    ambient_state.add_effect(effect)
elif output1_config['effect'] == 'flicker':
    effect = Flicker(output1, output1_config['seed'])
    effect.dim = output1_config['dim']
    ambient_state.add_effect(effect)

if output2_config['effect'] == 'dimmer':
    effect = Dimmer(output2)
    effect.dim = output2_config['dim']
    effect.strobe = output2_config['strobe']
    ambient_state.add_effect(effect)
elif output2_config['effect'] == 'flicker':
    effect = Flicker(output2, output2_config['seed'])
    effect.dim = output2_config['dim']
    ambient_state.add_effect(effect)

state_machine.add_state(ambient_state)

triggered_state = TriggeredState()
trigger_buffer = bytearray(1024)
trigger_wav = audiocore.WaveFile(TRIGGER_FILE, trigger_buffer)
triggered_state.set_i2s(i2s)
triggered_state.set_wave(trigger_wav)

try:
    output1_config = {'effect': 'dimmer', 'dim': 0, 'strobe': 0, 'seed': 0x55ce}
    output2_config = {'effect': 'dimmer', 'dim': 0, 'strobe': 0, 'seed': 0x6c4a}
    subtable = 'triggered'
    keys = cptoml.keys(subtable, toml=CONFIG_FILE)
    for key in keys:
        if key == 'effect1':
            output1_config['effect'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect1.dim':
            output1_config['dim'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect1.strobe':
            output1_confic['strobe'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect1.seed':
            output1_config['seed'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2':
            output2_config['effect'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2.dim':
            output2_config['dim'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2.strobe':
            output2_config['strobe'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
        elif key == 'effect2.seed':
            output2_config['seed'] = cptoml.fetch(key, subtable, toml=CONFIG_FILE)
except OSError as err:
    print("Application configuration not found:", err)

if output1_config['effect'] == 'dimmer':
    effect = Dimmer(output1)
    effect.dim = output1_config['dim']
    effect.strobe = output1_config['strobe']
    triggered_state.add_effect(effect)
elif output1_config['effect'] == 'flicker':
    effect = Flicker(output1, output1_config['seed'])
    effect.dim = output1_config['dim']
    triggered_state.add_effect(effect)

if output2_config['effect'] == 'dimmer':
    effect = Dimmer(output2)
    effect.dim = output2_config['dim']
    effect.strobe = output2_config['strobe']
    triggered_state.add_effect(effect)
elif output2_config['effect'] == 'flicker':
    effect = Flicker(output2, output2_config['seed'])
    effect.dim = output2_config['dim']
    triggered_state.add_effect(effect)

state_machine.add_state(triggered_state)

#dimmer = Dimmer(output1)
#dimmer.dim = 255
#ambient_state.add_effect(dimmer)

#flicker = Flicker(output2, 0x55ce)
#flicker.dim = 3
#ambient_state.add_effect(flicker)

#state_machine.add_state(ambient_state)

#triggered_state = TriggeredState()
#trigger_buffer = bytearray(1024)
#trigger_wav = audiocore.WaveFile(TRIGGER_FILE, trigger_buffer)
#triggered_state.set_i2s(i2s)
#triggered_state.set_wave(trigger_wav)

#dimmer = Dimmer(output1)
#dimmer.dim = 0
#dimmer.strobe = 1
#triggered_state.add_effect(dimmer)

#dimmer = Dimmer(output2)
#dimmer.dim = 255
#dimmer.strobe = 10
#triggered_state.add_effect(dimmer)

#state_machine.add_state(triggered_state)

state_machine.go_to_state('ambient')

print()
print("Electronic Actor Enhancement Controller")
print("Settings from config file: {0}".format(CONFIG_FILE))

#print('Upscale testing')
#x = 0x02
#r = upscale(x)
#print('Upscale {0:x}: {1:x}'.format(x, r))
#x = 0xff
#r = upscale(x)
#print('Upscale {0:x}: {1:x}'.format(x, r))

while True:
    trigger.update()
    state_machine.update()