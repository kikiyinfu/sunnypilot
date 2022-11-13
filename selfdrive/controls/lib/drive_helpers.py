import math

from cereal import car
from common.conversions import Conversions as CV
from common.numpy_fast import clip, interp
from common.params import Params
from common.realtime import DT_MDL, DT_CTRL
from selfdrive.modeld.constants import T_IDXS

# WARNING: this value was determined based on the model's training distribution,
#          model predictions above this speed can be unpredictable
V_CRUISE_MAX = 145  # kph
V_CRUISE_MIN = 8  # kph
V_CRUISE_MIN_HONDA = 5  # kph
V_CRUISE_DELTA_HONDA = 5
V_CRUISE_ENABLE_MIN_MPH = 32  # kph
V_CRUISE_ENABLE_MIN_KPH = 30  # kph
V_CRUISE_INITIAL = 255  # kph

MIN_SPEED = 1.0
LAT_MPC_N = 16
LON_MPC_N = 32
CONTROL_N = 17
CAR_ROTATION_RADIUS = 0.0

# EU guidelines
MAX_LATERAL_JERK = 5.0

ButtonEvent = car.CarState.ButtonEvent
ButtonType = car.CarState.ButtonEvent.Type
CRUISE_LONG_PRESS = 50
CRUISE_NEAREST_FUNC = {
  ButtonType.accelCruise: math.ceil,
  ButtonType.decelCruise: math.floor,
}
CRUISE_INTERVAL_SIGN = {
  ButtonType.accelCruise: +1,
  ButtonType.decelCruise: -1,
}


# Constants for Limit controllers.
LIMIT_ADAPT_ACC = -1.  # m/s^2 Ideal acceleration for the adapting (braking) phase when approaching speed limits.
LIMIT_MIN_ACC = -1.5  # m/s^2 Maximum deceleration allowed for limit controllers to provide.
LIMIT_MAX_ACC = 1.0   # m/s^2 Maximum acelration allowed for limit controllers to provide while active.
LIMIT_MIN_SPEED = 8.33  # m/s, Minimum speed limit to provide as solution on limit controllers.
LIMIT_SPEED_OFFSET_TH = -1.  # m/s Maximum offset between speed limit and current speed for adapting state.
LIMIT_MAX_MAP_DATA_AGE = 10.  # s Maximum time to hold to map data, then consider it invalid inside limits controllers.


class VCruiseHelper:
  def __init__(self, CP):
    self.CP = CP
    self.v_cruise_kph = V_CRUISE_INITIAL
    self.v_cruise_cluster_kph = V_CRUISE_INITIAL
    self.v_cruise_kph_last = 0
    self.button_timers = {ButtonType.decelCruise: 0, ButtonType.accelCruise: 0}
    self.button_change_states = {btn: {"standstill": False} for btn in self.button_timers}

    self.param_s = Params()
    self.accel_pressed = False
    self.decel_pressed = False
    self.accel_pressed_last = 0.
    self.decel_pressed_last = 0.
    self.fastMode = False
    self.reverse_acc_change = self.param_s.get_bool("ReverseAccChange")

  @property
  def v_cruise_initialized(self):
    return self.v_cruise_kph != V_CRUISE_INITIAL

  def update_v_cruise(self, CS, enabled_long, is_metric, sm):
    self.v_cruise_kph_last = self.v_cruise_kph

    self.reverse_acc_change = self.param_s.get_bool("ReverseAccChange")
    cur_time = sm.frame * DT_CTRL

    if CS.cruiseState.available:
      if not self.CP.pcmCruise or not self.CP.pcmCruiseSpeed:
        if CS.cruiseState.enabled:
          # if stock cruise is completely disabled, then we can use our own set speed logic
          if self.CP.carName == "honda":
            for b in CS.buttonEvents:
              if b.pressed:
                if b.type == ButtonType.accelCruise:
                  self.accel_pressed = True
                  self.accel_pressed_last = cur_time
                elif b.type == ButtonType.decelCruise:
                  self.decel_pressed = True
                  self.decel_pressed_last = cur_time
              else:
                if b.type == ButtonType.accelCruise:
                  self.accel_pressed = False
                elif b.type == ButtonType.decelCruise:
                  self.decel_pressed = False
            self._update_v_cruise_non_pcm_honda(CS, enabled_long, is_metric, cur_time)
            self.v_cruise_kph = self.v_cruise_kph if is_metric else int(round((float(round(self.v_cruise_kph)) - 0.0995) / 0.6233))
            self.v_cruise_cluster_kph = self.v_cruise_kph
            if self.accel_pressed or self.decel_pressed:
              if self.v_cruise_kph_last != self.v_cruise_kph:
                self.accel_pressed_last = cur_time
                self.decel_pressed_last = cur_time
                self.fastMode = True
            else:
              self.fastMode = False
          else:
            self._update_v_cruise_non_pcm(CS, enabled_long, is_metric)
            self.v_cruise_cluster_kph = self.v_cruise_kph
            self.update_button_timers(CS)
      else:
        self.v_cruise_kph = CS.cruiseState.speed * CV.MS_TO_KPH
        self.v_cruise_cluster_kph = CS.cruiseState.speedCluster * CV.MS_TO_KPH
    else:
      self.v_cruise_kph = V_CRUISE_INITIAL
      self.v_cruise_cluster_kph = V_CRUISE_INITIAL

  def _update_v_cruise_non_pcm(self, CS, enabled_long, is_metric):
    # handle button presses. TODO: this should be in state_control, but a decelCruise press
    # would have the effect of both enabling and changing speed is checked after the state transition
    if not enabled_long:
      return

    long_press = False
    button_type = None

    # should be CV.MPH_TO_KPH, but this causes rounding errors
    v_cruise_delta = 1. if is_metric else 1.6
    v_cruise_delta_multiplier = 10 if is_metric else 5

    for b in CS.buttonEvents:
      if b.type.raw in self.button_timers and not b.pressed:
        if self.button_timers[b.type.raw] > CRUISE_LONG_PRESS:
          return  # end long press
        button_type = b.type.raw
        break
    else:
      for k in self.button_timers.keys():
        if self.button_timers[k] and self.button_timers[k] % CRUISE_LONG_PRESS == 0:
          button_type = k
          long_press = True
          break

    if button_type is None:
      return

    # Don't adjust speed when pressing resume to exit standstill
    cruise_standstill = self.button_change_states[button_type]["standstill"] or CS.cruiseState.standstill
    if button_type == ButtonType.accelCruise and cruise_standstill:
      return

    if self.reverse_acc_change:
      v_cruise_delta = v_cruise_delta * (1 if long_press else v_cruise_delta_multiplier)
      if not long_press and self.v_cruise_kph % v_cruise_delta != 0:  # partial interval
        self.v_cruise_kph = CRUISE_NEAREST_FUNC[button_type](self.v_cruise_kph / v_cruise_delta) * v_cruise_delta
      else:
        self.v_cruise_kph += v_cruise_delta * CRUISE_INTERVAL_SIGN[button_type]
    else:
      v_cruise_delta = v_cruise_delta * (v_cruise_delta_multiplier if long_press else 1)
      if long_press and self.v_cruise_kph % v_cruise_delta != 0:  # partial interval
        self.v_cruise_kph = CRUISE_NEAREST_FUNC[button_type](self.v_cruise_kph / v_cruise_delta) * v_cruise_delta
      else:
        self.v_cruise_kph += v_cruise_delta * CRUISE_INTERVAL_SIGN[button_type]

    # If set is pressed while overriding, clip cruise speed to minimum of vEgo
    if CS.gasPressed and button_type in (ButtonType.decelCruise, ButtonType.setCruise):
      self.v_cruise_kph = max(self.v_cruise_kph, CS.vEgo * CV.MS_TO_KPH)

    self.v_cruise_kph = clip(round(self.v_cruise_kph, 1), V_CRUISE_MIN, V_CRUISE_MAX)

  def _update_v_cruise_non_pcm_honda(self, CS, enabled_long, is_metric, cur_time):

    self.v_cruise_kph = self.v_cruise_kph if is_metric else int(round((float(self.v_cruise_kph) * 0.6233 + 0.0995)))

    if enabled_long:
      if self.accel_pressed:
        if (cur_time - self.accel_pressed_last) >= 1 or (self.fastMode and (cur_time - self.accel_pressed_last) >= 0.5):
          if self.reverse_acc_change:
            self.v_cruise_kph += 1
          else:
            self.v_cruise_kph += V_CRUISE_DELTA_HONDA - (self.v_cruise_kph % V_CRUISE_DELTA_HONDA)
      elif self.decel_pressed:
        if (cur_time - self.decel_pressed_last) >= 1 or (self.fastMode and (cur_time - self.decel_pressed_last) >= 0.5):
          if self.reverse_acc_change:
            self.v_cruise_kph -= 1
          else:
            self.v_cruise_kph -= V_CRUISE_DELTA_HONDA - ((V_CRUISE_DELTA_HONDA - self.v_cruise_kph) % V_CRUISE_DELTA_HONDA)
      else:
        for b in CS.buttonEvents:
          if not b.pressed:
            if b.type == ButtonType.accelCruise:
              if not self.fastMode:
                if self.reverse_acc_change:
                  self.v_cruise_kph += V_CRUISE_DELTA_HONDA - (self.v_cruise_kph % V_CRUISE_DELTA_HONDA)
                else:
                  self.v_cruise_kph += 1
            elif b.type == ButtonType.decelCruise:
              if not self.fastMode:
                if self.reverse_acc_change:
                  self.v_cruise_kph -= V_CRUISE_DELTA_HONDA - ((V_CRUISE_DELTA_HONDA - self.v_cruise_kph) % V_CRUISE_DELTA_HONDA)
                else:
                  self.v_cruise_kph -= 1

          # If set is pressed while overriding, clip cruise speed to minimum of vEgo
          if CS.gasPressed and b.type in (ButtonType.decelCruise, ButtonType.setCruise):
            self.v_cruise_kph = max(self.v_cruise_kph, CS.vEgo * CV.MS_TO_KPH)

      self.v_cruise_kph = clip(self.v_cruise_kph, V_CRUISE_MIN_HONDA, V_CRUISE_MAX)

  def update_button_timers(self, CS):
    # increment timer for buttons still pressed
    for k in self.button_timers:
      if self.button_timers[k] > 0:
        self.button_timers[k] += 1

    for b in CS.buttonEvents:
      if b.type.raw in self.button_timers:
        # Start/end timer and store current state on change of button pressed
        self.button_timers[b.type.raw] = 1 if b.pressed else 0
        self.button_change_states[b.type.raw] = {"standstill": CS.cruiseState.standstill}

  def initialize_v_cruise(self, CS, is_metric):
    # initializing is handled by the PCM
    if self.CP.pcmCruise and self.CP.pcmCruiseSpeed:
      return

    # 250kph or above probably means we never had a set speed
    if any(b.type in (ButtonType.accelCruise, ButtonType.resumeCruise) for b in CS.buttonEvents) and self.v_cruise_kph_last < 250:
      self.v_cruise_kph = self.v_cruise_kph_last
    else:
      self.v_cruise_kph = int(round(clip(CS.vEgo * CV.MS_TO_KPH, V_CRUISE_ENABLE_MIN_KPH if is_metric else V_CRUISE_ENABLE_MIN_MPH, V_CRUISE_MAX)))

    self.v_cruise_cluster_kph = self.v_cruise_kph


def apply_deadzone(error, deadzone):
  if error > deadzone:
    error -= deadzone
  elif error < - deadzone:
    error += deadzone
  else:
    error = 0.
  return error


def rate_limit(new_value, last_value, dw_step, up_step):
  return clip(new_value, last_value + dw_step, last_value + up_step)


def get_lag_adjusted_curvature(CP, v_ego, psis, curvatures, curvature_rates):
  if len(psis) != CONTROL_N:
    psis = [0.0]*CONTROL_N
    curvatures = [0.0]*CONTROL_N
    curvature_rates = [0.0]*CONTROL_N
  v_ego = max(MIN_SPEED, v_ego)

  # TODO this needs more thought, use .2s extra for now to estimate other delays
  delay = CP.steerActuatorDelay + .2

  # MPC can plan to turn the wheel and turn back before t_delay. This means
  # in high delay cases some corrections never even get commanded. So just use
  # psi to calculate a simple linearization of desired curvature
  current_curvature_desired = curvatures[0]
  psi = interp(delay, T_IDXS[:CONTROL_N], psis)
  average_curvature_desired = psi / (v_ego * delay)
  desired_curvature = 2 * average_curvature_desired - current_curvature_desired

  # This is the "desired rate of the setpoint" not an actual desired rate
  desired_curvature_rate = curvature_rates[0]
  max_curvature_rate = MAX_LATERAL_JERK / (v_ego**2) # inexact calculation, check https://github.com/commaai/openpilot/pull/24755
  safe_desired_curvature_rate = clip(desired_curvature_rate,
                                     -max_curvature_rate,
                                     max_curvature_rate)
  safe_desired_curvature = clip(desired_curvature,
                                current_curvature_desired - max_curvature_rate * DT_MDL,
                                current_curvature_desired + max_curvature_rate * DT_MDL)

  return safe_desired_curvature, safe_desired_curvature_rate