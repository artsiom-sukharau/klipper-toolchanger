# Support for toolchnagers
#
# Copyright (C) 2023 Viesturs Zarins <viesturz@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast, bisect

STATUS_UNINITALIZED = 'uninitialized'
STATUS_INITIALIZING = 'initializing'
STATUS_READY = 'ready'
STATUS_CHANGING = 'changing'
STATUS_PAUSED = 'paused'
STATUS_ERROR = 'error'
INIT_ON_HOME = 0
INIT_MANUAL = 1
INIT_FIRST_USE = 2
XYZ_TO_INDEX = {'x': 0, 'X': 0, 'y': 1, 'Y': 1, 'z': 2, 'Z': 2}
INDEX_TO_XYZ = 'XYZ'

class Toolchanger:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')

        self.name = config.get_name()
        self.params = get_params_dict(config)
        init_options = {
            'home': INIT_ON_HOME,
            'manual': INIT_MANUAL,
            'first-use': INIT_FIRST_USE
        }
        self.initialize_on = config.getchoice('initialize_on', init_options, 'first-use')
        self.initialize_gcode = self.gcode_macro.load_template(config, 'initialize_gcode', '')
        self.before_change_gcode = self.gcode_macro.load_template(config, 'before_change_gcode', '')
        self.after_change_gcode = self.gcode_macro.load_template(config, 'after_change_gcode', '')
        self.finalize_after_change_gcode = self.gcode_macro.load_template(config, 'finalize_after_change_gcode', '')

        # Read all the fields that might be defined on toolchanger.
        # To avoid throwing config error when no tools configured.
        config.get('pickup_gcode', None)
        config.get('dropoff_gcode', None)
        config.getfloat('gcode_x_offset', None)
        config.getfloat('gcode_y_offset', None)
        config.getfloat('gcode_z_offset', None)
        config.get('t_command_restore_axis', None)
        config.get('extruder', None)
        config.get('fan', None)
        config.get_prefix_options('params_')

        self.status = STATUS_UNINITALIZED
        self.active_tool = None
        self.tools = {}
        self.tool_numbers = [] # Ordered list of registered tool numbers.
        self.tool_names = [] # Tool names, in the same order as numbers.
        self.error_message = ''

        self.drop_docksense_corrections = {}
        self.pick_docksense_corrections = {}

        self.last_dropoff_tool = None
        self.last_pickup_tool = None
        self.last_restore_position = None

        self.printer.register_event_handler("homing:home_rails_begin", self._handle_home_rails_begin)

        self.gcode.register_command("TTC_INITIALIZE_TOOLCHANGER",
                                    self.cmd_INITIALIZE_TOOLCHANGER,
                                    desc=self.cmd_INITIALIZE_TOOLCHANGER_help)
        self.gcode.register_command("TTC_SET_TOOL_TEMPERATURE",
                                    self.cmd_SET_TOOL_TEMPERATURE,
                                    desc=self.cmd_SET_TOOL_TEMPERATURE_help)
        self.gcode.register_command("TTC_SELECT_TOOL",
                                   self.cmd_SELECT_TOOL,
                                   desc=self.cmd_SELECT_TOOL_help)
        self.gcode.register_command("TTC_SELECT_TOOL_ERROR",
                                    self.cmd_SELECT_TOOL_ERROR,
                                    desc=self.cmd_SELECT_TOOL_ERROR_help)
        self.gcode.register_command("TTC_UNSELECT_TOOL",
                                    self.cmd_UNSELECT_TOOL,
                                    desc=self.cmd_UNSELECT_TOOL_help)
        self.gcode.register_command("TTC_TEST_TOOL_DOCKING",
                                    self.cmd_TEST_TOOL_DOCKING,
                                    desc=self.cmd_TEST_TOOL_DOCKING_help)
        self.gcode.register_command("TTC_SET_TOOL_PARAMETER",
                                    self.cmd_SET_TOOL_PARAMETER)
        self.gcode.register_command("TTC_RESET_TOOL_PARAMETER",
                                    self.cmd_RESET_TOOL_PARAMETER)
        self.gcode.register_command("TTC_SAVE_TOOL_PARAMETER",
                                    self.cmd_SAVE_TOOL_PARAMETER)
        # @Tem
        self.gcode.register_command("TTC_PAUSE_DETAILS",
                                    self.cmd_PAUSE_DETAILS,
                                    desc=self.cmd_PAUSE_DETAILS_help)
        self.gcode.register_command("TTC_PAUSE_RESOLVE",
                                    self.cmd_PAUSE_RESOLVE,
                                    desc=self.cmd_PAUSE_RESOLVE_help)
        #
        self.gcode.register_command("TTC_SET_TOOL_GCODE_X_OFFSET",
                                    self.cmd_SET_TOOL_GCODE_X_OFFSET,
                                    desc=self.cmd_SET_TOOL_GCODE_X_OFFSET_help)
        self.gcode.register_command("TTC_SET_TOOL_GCODE_Y_OFFSET",
                                    self.cmd_SET_TOOL_GCODE_Y_OFFSET,
                                    desc=self.cmd_SET_TOOL_GCODE_Y_OFFSET_help)
        self.gcode.register_command("TTC_SET_TOOL_GCODE_Z_OFFSET",
                                    self.cmd_SET_TOOL_GCODE_Z_OFFSET,
                                    desc=self.cmd_SET_TOOL_GCODE_Z_OFFSET_help)
        self.gcode.register_command("TTC_SAVE_TOOL_GCODE_OFFSETS",
                                    self.cmd_SAVE_TOOL_GCODE_OFFSETS,
                                    desc=self.cmd_SAVE_TOOL_GCODE_OFFSETS_help)
        # @Tem
        self.gcode.register_command("TEST_MACROS_RUNNING",
                                    self.cmd_TEST_MACROS_RUNNING)
        # @TODO: remove it later. Legacy
        self.gcode.register_command("DROPOFF_WITH_DOCK_CHECK",
                                    self.cmd_DROPOFF_WITH_DOCK_CHECK)

    def _handle_home_rails_begin(self, homing_state, rails):
        if self.initialize_on == INIT_ON_HOME and self.status == STATUS_UNINITALIZED:
            self.initialize()

    def get_status(self, eventtime):
        return {** self.params,
                'name': self.name,
                'status': self.status,
                'tool': self.active_tool.name if self.active_tool else None,
                'tool_number': self.active_tool.tool_number if self.active_tool else -1,
                'tool_numbers': self.tool_numbers,
                'tool_names': self.tool_names,
                }

    def assign_tool(self, tool, number, prev_number, replace = False):
        if number in self.tools and not replace:
            raise Exception('Duplicate tools with number %s' % (str(number)))
        if prev_number in self.tools:
            del self.tools[prev_number]
            self.tool_numbers.remove(prev_number)
            self.tool_names.remove(tool.name)
        self.tools[number] = tool
        position = bisect.bisect_left(self.tool_numbers, number)
        self.tool_numbers.insert(position, number)
        self.tool_names.insert(position, tool.name)

    def _get_tool_from_gcmd(self, gcmd):
        tool_name = gcmd.get('TOOL', None)
        tool_nr = gcmd.get_int('T', None)
        if tool_name:
            tool = self.printer.lookup_object(tool_name)
        elif tool_nr is not None:
            tool = self.lookup_tool(tool_nr)
            if not tool:
                raise gcmd.error("SET_TOOL_TEMPERATURE: T%d not found" % (tool_nr))
        else:
            tool = self.active_tool
            if not tool:
                raise gcmd.error("SET_TOOL_TEMPERATURE: No tool specified and no active tool")
        return tool

    cmd_INITIALIZE_TOOLCHANGER_help = "Initialize the toolchanger"
    def cmd_INITIALIZE_TOOLCHANGER(self, gcmd):
        tool_name = gcmd.get('TOOL', None)
        tool_number = gcmd.get_int('T', None)
        tool = None
        if tool_name:
            tool = self.printer.lookup_object(tool_name)
        if tool_number is not None:
            tool = self.lookup_tool(tool_number)
            if not tool:
                raise gcmd.error('Tool #%d is not assigned' % (tool_number))
        self.initialize(tool)

    cmd_SELECT_TOOL_help = 'Select active tool'
    def cmd_SELECT_TOOL(self, gcmd):
        tool_name = gcmd.get('TOOL', None)
        if tool_name:
            tool = self.printer.lookup_object(tool_name)
            restore_axis = gcmd.get('RESTORE_AXIS', tool.t_command_restore_axis)
            self.select_tool(gcmd, tool, restore_axis)
            return
        tool_nr = gcmd.get_int('T', None)
        if tool_nr is not None:
            tool = self.lookup_tool(tool_nr)
            if not tool:
                raise gcmd.error("Select tool: T%d not found" % (tool_nr))
            restore_axis = gcmd.get('RESTORE_AXIS', tool.t_command_restore_axis)
            self.select_tool(gcmd, tool, restore_axis)
            return
        raise gcmd.error("Select tool: Either TOOL or T needs to be specified")

    cmd_SET_TOOL_TEMPERATURE_help = 'Set temperature for tool'
    def cmd_SET_TOOL_TEMPERATURE(self, gcmd):
        temp = gcmd.get_float('TARGET', 0.)
        wait = gcmd.get_int('WAIT', 0) == 1
        tool = self._get_tool_from_gcmd(gcmd)
        if not tool.extruder:
            raise gcmd.error("SET_TOOL_TEMPERATURE: No extruder specified for tool %s" % (tool.name))
        heaters = self.printer.lookup_object('heaters')
        heaters.set_temperature(tool.extruder.get_heater(), temp, wait)

    cmd_SELECT_TOOL_ERROR_help = "Abort tool change and mark the active toolchanger as failed"
    def cmd_SELECT_TOOL_ERROR(self, gcmd):
        if self.status != STATUS_CHANGING and self.status != STATUS_INITIALIZING:
            gcmd.respond_info('SELECT_TOOL_ERROR called while not selecting, doing nothing')
            return
        self.status = STATUS_ERROR
        self.error_message = gcmd.get('MESSAGE', '')

    cmd_UNSELECT_TOOL_help = "Unselect active tool without selecting a new one"
    def cmd_UNSELECT_TOOL(self, gcmd):
        if not self.active_tool:
            return
        restore_axis = gcmd.get('RESTORE_AXIS', self.active_tool.t_command_restore_axis)
        self.select_tool(gcmd, None, restore_axis)

    cmd_TEST_TOOL_DOCKING_help = "Unselect active tool and select it again"
    def cmd_TEST_TOOL_DOCKING(self, gcmd):
        if not self.active_tool:
            raise gcmd.error("Cannot test tool, no active tool")
        restore_axis = gcmd.get('RESTORE_AXIS', self.active_tool.t_command_restore_axis)
        self.test_tool_selection(gcmd, restore_axis)

    def cmd_SET_TOOL_PARAMETER(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        name = gcmd.get("PARAMETER")
        if name in tool.params and name not in tool.original_params:
            tool.original_params[name] = tool.params[name]
        value = ast.literal_eval(gcmd.get("VALUE"))
        tool.params[name] = value

    def cmd_RESET_TOOL_PARAMETER(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        name = gcmd.get("PARAMETER")
        if name in tool.original_params:
            tool.params[name] = tool.original_params[name]

    def cmd_SAVE_TOOL_PARAMETER(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        name = gcmd.get("PARAMETER")
        if name not in tool.params:
            raise gcmd.error('Tool does not have parameter %s' % (name))
        configfile = self.printer.lookup_object('configfile')
        configfile.set(tool.name, name, tool.params[name])

    # @Tem
    cmd_PAUSE_RESOLVE_help = "Initialize the toolchanger after pause and resume print."
    def cmd_PAUSE_RESOLVE(self, gcmd):
        tool_number = gcmd.get_int('T', None)
        tool = None
        if tool_number is not None:
          tool = self.lookup_tool(tool_number)

        if not tool:
          gcmd.respond_info('Tool T%s was not found.' % (tool_number))

        self.execute_toolchange_pause_resolve(tool)

    cmd_PAUSE_DETAILS_help = "Prints deatils about pause."
    def cmd_PAUSE_DETAILS(self, gcmd):
        self.toolchange_pause_print_details(gcmd)

    cmd_SET_TOOL_GCODE_X_OFFSET_help = "Set gcode_x_offset for current tool."
    def cmd_SET_TOOL_GCODE_X_OFFSET(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        value = ast.literal_eval(gcmd.get("VALUE"))
        tool.gcode_x_offset = value

    cmd_SET_TOOL_GCODE_Y_OFFSET_help = "Set gcode_y_offset for current tool."
    def cmd_SET_TOOL_GCODE_Y_OFFSET(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        value = ast.literal_eval(gcmd.get("VALUE"))
        tool.gcode_y_offset = value

    cmd_SET_TOOL_GCODE_Z_OFFSET_help = "Set gcode_z_offset for current tool."
    def cmd_SET_TOOL_GCODE_Z_OFFSET(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        value = ast.literal_eval(gcmd.get("VALUE"))
        tool.gcode_z_offset = value

    cmd_SAVE_TOOL_GCODE_OFFSETS_help = "Saves current tool gcode offsets to config file."
    def cmd_SAVE_TOOL_GCODE_OFFSETS(self, gcmd):
        tool = self._get_tool_from_gcmd(gcmd)
        configfile = self.printer.lookup_object('configfile')
        configfile.set(tool.name, 'gcode_x_offset', tool.gcode_x_offset)
        configfile.set(tool.name, 'gcode_y_offset', tool.gcode_y_offset)
        configfile.set(tool.name, 'gcode_z_offset', tool.gcode_z_offset)

    def cmd_TEST_MACROS_RUNNING(self, gcmd):
        # ----------------------------------------
        # gcode_button = self.printer.lookup_object('gcode_button carriagesense_t0')
        # state = gcode_button.get_status()['state']
        # gcmd.respond_info("gcode_button carriagesense_t0 = " + state)

        # ----------------------------------------
        # gcode_button = self.printer.lookup_object('gcode_button docksense_t0')
        # state = gcode_button.get_status()['state']
        # gcmd.respond_info("gcode_button docksense_t0 = " + state)

        # ----------------------------------------
        # gcmd.respond_info("TEST_MACROS_RUNNING started...")
        # self.run_gcode_from_command("TTT_LONG_RUNNING_MC")
        # self.run_gcode_from_command("TTT_FAST_RUNNING_MC")
        # gcmd.respond_info("TEST_MACROS_RUNNING done...")

        # ----------------------------------------
        # tool = self.lookup_tool(0)
        # for par in tool.params:
        #   gcmd.respond_info("-->> T%s %s=%s" % (0, par, tool.params[par]))
        # gcmd.respond_info("-->> T%s [params_park_x=%s]" % (0, tool.params['params_park_x']))

        # ----------------------------------------
        # curtime = self.printer.get_reactor().monotonic()
        # end_parking_speed = self.printer.lookup_object('gcode_macro _TOOLCHANGER_CONFIGURATION').get_status(curtime)['end_parking_speed']
        # gcmd.respond_info("end_parking_speed = %s" % (end_parking_speed))

        # ----------------------------------------
        # --toolhead = self.printer.lookup_object('toolhead')
        # --gcmd.respond_info("toolhead homed_axes = %s", toolhead.homed_axes)

        # ----------------------------------------
        # curtime = self.printer.get_reactor().monotonic()
        # is_ltc_paused = self.printer.lookup_object('gcode_macro _LTC_PAUSE').get_status(curtime)['is_ltc_paused']
        # gcmd.respond_info("is_ltc_paused = %s" % (is_ltc_paused))
        # #
        # cmd = self.gcode.create_gcode_command("SET_GCODE_VARIABLE", "SET_GCODE_VARIABLE", {
        #   'VARIABLE': 'is_ltc_paused',
        #   'VALUE': 1
        # })
        # self.printer.lookup_object('gcode_macro _LTC_PAUSE').cmd_SET_GCODE_VARIABLE(cmd)
        # curtime = self.printer.get_reactor().monotonic()
        # #
        # is_ltc_paused = self.printer.lookup_object('gcode_macro _LTC_PAUSE').get_status(curtime)['is_ltc_paused']
        # gcmd.respond_info("is_ltc_paused = %s" % (is_ltc_paused))

        # ----------------------------------------
        # self.printer.lookup_object('configfile')

        # ----------------------------------------
        # curtime = self.printer.get_reactor().monotonic()
        # toolhead = self.printer.lookup_object('toolhead')
        # status = toolhead.get_kinematics().get_status(curtime)
        # # x_min = status["axis_minimum"][0]
        # # y_min = status["axis_minimum"][1]
        # # z_min = status["axis_minimum"][2]
        # x_min, y_min, z_min, _ = status["axis_minimum"]
        # gcmd.respond_info("x_min = %s, y_min = %s, z_min = %s" % (x_min, y_min, z_min))
        # # x_max = status["axis_maximum"][0]
        # # y_max = status["axis_maximum"][1]
        # # z_max = status["axis_maximum"][2]
        # x_max, y_max, z_max, _ = status["axis_maximum"]
        # gcmd.respond_info("x_max = %s, y_max = %s, z_max = %s" % (x_max, y_max, z_max))

        # ----------------------------------------
        # toolhead = self.printer.lookup_object('toolhead')
        # position = toolhead.get_position()
        # # cur_x = position[0]
        # # cur_y = position[1]
        # # cur_z = position[2]
        # cur_x, cur_y, cur_z, cur_e = position
        # gcmd.respond_info("cur_x = %s, cur_y = %s, cur_z = %s" % (cur_x, cur_y, cur_z))

        # ----------------------------------------
        # end_parking_speed = self.get_macro_var('_TOOLCHANGER_CONFIGURATION', 'end_parking_speed')
        # gcmd.respond_info("end_parking_speed = %s" % (end_parking_speed))
        # is_ltc_paused = self.get_macro_var('_LTC_PAUSE', 'is_ltc_paused')
        # gcmd.respond_info("is_ltc_paused = %s" % (is_ltc_paused))

        # ----------------------------------------
        # is_ltc_paused = self.get_macro_var('_LTC_PAUSE', 'is_ltc_paused')
        # gcmd.respond_info("is_ltc_paused = %s" % (is_ltc_paused))
        # self.save_macro_var('_LTC_PAUSE', 'is_ltc_paused', 1)
        # is_ltc_paused = self.get_macro_var('_LTC_PAUSE', 'is_ltc_paused')
        # gcmd.respond_info("is_ltc_paused = %s" % (is_ltc_paused))

        # ----------------------------------------
        # cfg = self.get_macro_vars('_TOOLCHANGER_CONFIGURATION')
        # gcmd.respond_info("end_parking_speed = %s" % (cfg['end_parking_speed']))

        # ----------------------------------------
        # for tool_number in self.printer.lookup_object('toolchanger').tool_numbers:
        #   gcmd.respond_info("tool: number = %s, name = %s" % (tool_number, self.lookup_tool(tool_number).name))


        # ----------------------------------------
        # tool = self.lookup_tool(0)
        # step = 0.5
        # if tool.tool_number not in self.drop_docksense_corrections:
        #   self.drop_docksense_corrections[tool.tool_number] = []
        #   for i in range(200):
        #      self.drop_docksense_corrections[tool.tool_number].append(step)

        #   # reduce list
        #   if len(self.drop_docksense_corrections[tool.tool_number]) > 100:
        #       avg_correction = sum(self.drop_docksense_corrections[tool.tool_number]) / len(self.drop_docksense_corrections[tool.tool_number])
        #       self.drop_docksense_corrections[tool.tool_number] = []
        #       self.drop_docksense_corrections[tool.tool_number].append(avg_correction)

        #   self.drop_docksense_corrections[tool.tool_number].append(step)
        #   if gcmd:
        #     gcmd.respond_info("Toolhead T%s average step correction=%s." %
        #       (tool.tool_number, sum(self.drop_docksense_corrections[tool.tool_number]) / len(self.drop_docksense_corrections[tool.tool_number]))
        #     )

        # ---------------------------------------- DOENT WORK
        # configfile = self.printer.lookup_object('configfile')
        # cfg = configfile.getsection('tmc5160 stepper_x')
        # gcmd.respond_info("tmc5160 stepper_x run_current= %s" % (run_current))

        # run_current, hold_current, *rest = self.printer.lookup_object('tmc5160 stepper_x').current_helper.get_current()
        # gcmd.respond_info("tmc5160 stepper_x run_current= %s" % (run_current))

        # https://github.com/Klipper3d/klipper/blob/8a3d2afd796414d64b995ac753148484b77198dd/klippy/extras/print_stats.py#L48
        # curtime = self.printer.get_reactor().monotonic()
        # print_stats = self.printer.lookup_object('print_stats').get_status(curtime)
        # gcmd.respond_info("print_stats state= %s" % (print_stats['state']))

        tool = self.lookup_tool(0)
        curtime = self.printer.get_reactor().monotonic()
        temp, target_temp = tool.extruder.get_heater().get_temp(curtime)
        gcmd.respond_info("tool temp temp=%s target_temp=%s" % (temp, target_temp))

    # @TODO: Backward compatibility, remove it.
    def cmd_DROPOFF_WITH_DOCK_CHECK(self, gcmd):
      if self.status == STATUS_PAUSED:
        return False

      tool = self._get_tool_from_gcmd(gcmd)
      self._drop_tool_with_dock_check(tool, None, gcmd)

    def cmd_DROPOFF_WITH_CARRIAGE_CHECK(self, gcmd):
      if self.status == STATUS_PAUSED:
        return

      tool = self._get_tool_from_gcmd(gcmd)
      self.drop_tool_with_carriage_check(tool, {}, gcmd)

    def initialize(self, select_tool=None):
        if self.status == STATUS_CHANGING:
            raise Exception('Cannot initialize while changing tools.')

        # Initialize may be called from within the intialize gcode
        # to set active tool without performing a full change
        should_run_initialize = self.status != STATUS_INITIALIZING

        if should_run_initialize:
            self.status = STATUS_INITIALIZING
            self.run_gcode('initialize_gcode', self.initialize_gcode, {})

        if select_tool:
            self._configure_toolhead_for_tool(select_tool)

            # self.run_gcode('after_change_gcode', self.after_change_gcode, {})
            # @TODO: New code, replace with this code.
            if (not self.after_change(None, None)):
              raise Exception("Cannot initialize the tool, toolchanger status is " + self.status)

            self._set_tool_gcode_offset(select_tool)
            # self.run_gcode('finalize_after_change_gcode', self.finalize_after_change_gcode, {})
            # @TODO: New code, replace with this code.
            if (not self.finalize_after_change(None, None)):
               raise Exception("Cannot initialize the tool, toolchanger status is " + self.status)

        if should_run_initialize:
            if self.status == STATUS_INITIALIZING:
                self.status = STATUS_READY
                self.gcode.respond_info('%s initialized, active %s' %
                                        (self.name, self.active_tool.name if self.active_tool else None))
            else:
                raise self.gcode.error('%s failed to initialize, error: %s' %
                                        (self.name, self.error_message))

    def select_tool(self, gcmd, tool, restore_axis):
      try:
        extra_context_obj = None
        if self.status == STATUS_UNINITALIZED and self.initialize_on == INIT_FIRST_USE:
            self.initialize()

        if self.status != STATUS_READY:
            raise gcmd.error("Cannot select tool, toolchanger status is " + self.status)

        if self.active_tool == tool:
            gcmd.respond_info('Tool %s already selected' % tool.name if tool else None)
            return

        self.status = STATUS_CHANGING
        gcode_position = self.gcode_move.get_status()['gcode_position']

        # extra_context = {
        #     'dropoff_tool': self.active_tool.name if self.active_tool else None,
        #     'pickup_tool': tool.name if tool else None,
        #     'restore_position': self._restore_position_with_tool_offset(
        #         gcode_position, restore_axis, tool)
        # }

        extra_context_obj = {
            'dropoff_tool': self.active_tool if self.active_tool else None,
            'pickup_tool': tool if tool else None,
            'restore_position': self._restore_position_with_tool_offset(
                gcode_position, restore_axis, tool)
        }

        self.run_gcode_from_command("SAVE_GCODE_STATE NAME=_toolchange_state")
        # self.run_gcode('before_change_gcode', self.before_change_gcode, extra_context)
        # @TODO: New code, replace with this code.
        if (not self.before_change(extra_context_obj, gcmd)):
          raise Exception("Cannot change the tool, toolchanger status is " + self.status)

        self.run_gcode_from_command("SET_GCODE_OFFSET X=0.0 Y=0.0 Z=0.0")

        if self.active_tool:
            # self.run_gcode('tool.dropoff_gcode', self.active_tool.dropoff_gcode, extra_context)
            # @TODO: New code, replace with this code.
            if (not self.dropoff_gcode(extra_context_obj, gcmd)):
              raise gcmd.error("Cannot drop the tool, toolchanger status is " + self.status)

        if tool is not None:
            self._configure_toolhead_for_tool(tool)
            # self.run_gcode('tool.pickup_gcode', tool.pickup_gcode, extra_context)
            # @TODO: New code, replace with this code.
            if (not self.pickup_gcode(extra_context_obj, gcmd)):
                raise gcmd.error("Cannot pick up the tool, toolchanger status is " + self.status)

            # self.run_gcode('after_change_gcode', self.after_change_gcode, extra_context)
            # @TODO: New code, replace with this code.
            if (not self.after_change(extra_context_obj, gcmd)):
                raise gcmd.error("Cannot pick up the tool, toolchanger status is " + self.status)

        self._restore_axis(gcode_position, restore_axis, tool)

        self.run_gcode_from_command("RESTORE_GCODE_STATE NAME=_toolchange_state MOVE=0")

        # Restore state sets old gcode offsets, fix that.
        if tool is not None:
            self._set_tool_gcode_offset(tool)
            # self.run_gcode('finalize_after_change_gcode', self.finalize_after_change_gcode, extra_context)
            # @TODO: New code, replace with this code.
            if (not self.finalize_after_change(extra_context_obj, gcmd)):
              raise Exception("Cannot pick up the tool, toolchanger status is " + self.status)


        self.status = STATUS_READY
        if tool:
            gcmd.respond_info('Selected tool %s (%s)' % (str(tool.tool_number), tool.name))
        else:
            gcmd.respond_info('Tool unselected')
      except Exception as e:
        gcmd.respond_info('!!! Exception while selecting tool: %s' % e)
        gcmd.respond_info('!!! Exception args: %s' % e.args)
        cur_x, cur_y, cur_z, cur_e = self.get_current_position()
        ctx = extra_context_obj if extra_context_obj is not None else {
          'dropoff_tool': self.active_tool if self.active_tool else tool,
          'pickup_tool': tool,
          'restore_position': {'X': cur_x, 'Y': cur_y, 'Z': cur_z}
        }
        self.execute_toolchange_pause(ctx, gcmd)
        return

    def test_tool_selection(self, gcmd, restore_axis):
        if self.status != STATUS_READY:
            raise gcmd.error("Cannot test tool, toolchanger status is " + self.status)
        tool = self.active_tool
        if not tool:
            raise gcmd.error("Cannot test tool, no active tool")

        self.status = STATUS_CHANGING
        gcode_position = self.gcode_move.get_status()['gcode_position']
        extra_context = {
            'dropoff_tool': self.active_tool.name if self.active_tool else None,
            'pickup_tool': tool.name if tool else None,
            'restore_position': self._restore_position_with_tool_offset(gcode_position, restore_axis, tool)
        }

        self.run_gcode_from_command("SET_GCODE_OFFSET X=0.0 Y=0.0 Z=0.0")
        self.run_gcode('tool.dropoff_gcode', self.active_tool.dropoff_gcode, extra_context)
        self.run_gcode('tool.pickup_gcode', tool.pickup_gcode, extra_context)

        self._restore_axis(gcode_position, restore_axis, None)
        self.status = STATUS_READY
        gcmd.respond_info('Tool testing done')

    def lookup_tool(self, number):
        return self.tools.get(number, None)

    def get_selected_tool(self):
        return self.active_tool

    def _configure_toolhead_for_tool(self, tool):
        if self.active_tool:
            self.active_tool.deactivate()
        self.active_tool = tool
        if self.active_tool:
            self.active_tool.activate()

    def _set_tool_gcode_offset(self, tool):
        if tool is None:
            return
        if tool.gcode_x_offset is None and tool.gcode_y_offset is None and tool.gcode_z_offset is None:
            return
        cmd = 'SET_GCODE_OFFSET'
        if tool.gcode_x_offset is not None:
            cmd += ' X=%f' % (tool.gcode_x_offset,)
        if tool.gcode_y_offset is not None:
            cmd += ' Y=%f' % (tool.gcode_y_offset,)
        if tool.gcode_z_offset is not None:
            cmd += ' Z=%f' % (tool.gcode_z_offset,)
        self.run_gcode_from_command(cmd)
        mesh = self.printer.lookup_object('bed_mesh')
        if mesh and mesh.get_mesh():
            self.run_gcode_from_command('BED_MESH_OFFSET X=%.6f Y=%.6f' %
                                                (-tool.gcode_x_offset, -tool.gcode_y_offset))

    def _restore_position_with_tool_offset(self, position, axis, tool):
        result = {}
        for i in axis:
            index = XYZ_TO_INDEX[i]
            v = position[index]
            if tool:
                offset = 0.
                if index == 0:
                    offset = tool.gcode_x_offset
                elif index == 1:
                    offset = tool.gcode_y_offset
                elif index == 2:
                    offset = tool.gcode_z_offset
                v += offset
            result[INDEX_TO_XYZ[index]] = v
        return result

    def _restore_axis(self, position, axis, tool):
        if not axis:
            return
        pos = self._restore_position_with_tool_offset(position, axis, tool)
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", pos))

    def run_gcode(self, name, template, extra_context={}):
        current_status = self.status
        # if current_status == STATUS_PAUSED:
        #   return

        curtime = self.printer.get_reactor().monotonic()
        try:
            context = {
                **template.create_template_context(),
                'tool': self.active_tool.get_status(curtime) if self.active_tool else {},
                'toolchanger': self.get_status(curtime),
                **extra_context,
            }
            template.run_gcode_from_command(context)
        except Exception as e:
            raise Exception("Script running error: %s" % (str(e)))
        if current_status != self.status:
            raise Exception("Unexpected status during %s, status = %s, message = %s, aborting" % (
                name, self.status, self.error_message))

    def run_gcode_from_command(self, command):
        current_status = self.status
        curtime = self.printer.get_reactor().monotonic()
        # if current_status == STATUS_PAUSED:
        #   return

        self.gcode.run_script_from_command(command)

    #
    #
    #

    def get_printer_status(self):
      curtime = self.printer.get_reactor().monotonic()
      print_stats = self.printer.lookup_object('print_stats').get_status(curtime)
      return print_stats['state']

    def get_printer_homed_axes(self):
      curtime = self.printer.get_reactor().monotonic()
      toolhead = self.printer.lookup_object('toolhead')
      status = toolhead.get_kinematics().get_status(curtime)
      return status["homed_axes"] # string

    def get_current_position(self):
      toolhead = self.printer.lookup_object('toolhead')
      return toolhead.get_position()

    def get_axis_minimum(self):
      curtime = self.printer.get_reactor().monotonic()
      toolhead = self.printer.lookup_object('toolhead')
      status = toolhead.get_kinematics().get_status(curtime)
      return status["axis_minimum"]

    def get_axis_maximum(self):
      curtime = self.printer.get_reactor().monotonic()
      toolhead = self.printer.lookup_object('toolhead')
      status = toolhead.get_kinematics().get_status(curtime)
      return status["axis_maximum"]

    def is_in_priniting_state(self):
      return self.get_printer_status() == 'printing'

    def is_in_paused_state(self):
      return self.get_printer_status() == 'paused'

    def get_tool_temps(self, tool):
      curtime = self.printer.get_reactor().monotonic()
      return tool.extruder.get_heater().get_temp(curtime)

    def get_macro_vars(self, macro_name, curtime = None):
      curtime = curtime if curtime else self.printer.get_reactor().monotonic()
      return self.printer.lookup_object("gcode_macro %s" % macro_name).get_status(curtime)

    def get_macro_var(self, macro_name, variable_name, default = None, curtime = None):
      macro_vars = self.get_macro_vars(macro_name, curtime)
      return macro_vars[variable_name] if (macro_vars is not None) and (macro_vars[variable_name] is not None) else default

    def save_macro_var(self, macro_name, variable_name, value):
      self.printer.lookup_object("gcode_macro %s" % macro_name).cmd_SET_GCODE_VARIABLE(
        self.gcode.create_gcode_command("SET_GCODE_VARIABLE", "SET_GCODE_VARIABLE", {
          'VARIABLE': variable_name, 'VALUE': value
        })
      )

    def _set_toolhead_temperature(self, tool, delta = 0, wait = False, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if (self.is_in_priniting_state() or self.is_in_paused_state()):
        tool_name = 'T%s' % tool.tool_number
        nozzle_temperature_initial = self.get_macro_var(tool_name, 'nozzle_temperature_initial', 0)
        nozzle_temperature = self.get_macro_var(tool_name, 'nozzle_temperature', 0)
        is_primed = self.get_macro_var('PRINT_START', 'is_primed', 0)
        # wait_temperature_delta = self.get_macro_var('_TOOLCHANGER_CONFIGURATION', 'wait_temperature_delta', 0)
        if (not is_primed) and (nozzle_temperature_initial != 0):
          if gcmd: gcmd.respond_info("Set temperature: tool=%s to INITIAL t=%s" % (tool.tool_number, nozzle_temperature_initial - delta))
          if wait:
            self.run_gcode_from_command('M109 T%s S%s' % (tool.tool_number, nozzle_temperature_initial - delta))
          else:
            self.run_gcode_from_command('M104 T%s S%s' % (tool.tool_number, nozzle_temperature_initial - delta))
        elif (is_primed) and (nozzle_temperature != 0):
          if gcmd: gcmd.respond_info("Set temperature: tool=%s to t=%s" % (tool.tool_number, nozzle_temperature - delta))
          if wait:
            self.run_gcode_from_command('M109 T%s S%s' % (tool.tool_number, nozzle_temperature - delta))
          else:
            self.run_gcode_from_command('M104 T%s S%s' % (tool.tool_number, nozzle_temperature - delta))

      return True

    #
    #
    #

    def check_dock_state(self, tool, state):
      gcode_button = self.printer.lookup_object('gcode_button docksense_t%s' % (tool.tool_number))
      current_state = gcode_button.get_status()['state']
      return current_state == state

    def check_carriage_state(self, tool, state):
      gcode_button = self.printer.lookup_object('gcode_button carriagesense_t%s' % (tool.tool_number))
      current_state = gcode_button.get_status()['state']
      return current_state == state

    def before_change(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling before_change...")

      dropoff_tool = context['dropoff_tool']
      pickup_tool = context['pickup_tool']

      if gcmd: gcmd.respond_info(
         "Changing tool form tool=T%s to tool=T%s" %
         (dropoff_tool.tool_number, pickup_tool.tool_number))

      restore_position = context['restore_position']
      restore_position_x = restore_position['X'] if 'X' in restore_position else None
      restore_position_y = restore_position['Y'] if 'Y' in restore_position else None
      restore_position_z = restore_position['Z'] if 'Z' in restore_position else None
      if gcmd: gcmd.respond_info(
         "Restore position: x=%s y=%s z=%s" %
         (restore_position_x, restore_position_y, restore_position_z))

      # @TODO: remove it later. Legacy
      self.save_macro_var('_LTC_PAUSE', 'restore_position_x', restore_position_x if restore_position_x else -1)
      self.save_macro_var('_LTC_PAUSE', 'restore_position_y', restore_position_y if restore_position_y else -1)
      self.save_macro_var('_LTC_PAUSE', 'restore_position_z', restore_position_z if restore_position_z else -1)
      self.save_macro_var('_LTC_PAUSE', 'restore_position_saved', 1)

      # @TODO: remove it later. Legacy
      self.save_macro_var('_LTC_PAUSE', 'dropoff_tool_number', dropoff_tool.tool_number)
      self.save_macro_var('_LTC_PAUSE', 'pickup_tool_number', pickup_tool.tool_number)

      self.last_dropoff_tool = context['dropoff_tool']
      self.last_pickup_tool = context['pickup_tool']
      self.last_restore_position = context['restore_position']

      self.run_gcode_from_command("SAVE_GCODE_STATE NAME=BEFORE_TOOL_CHANGE_STATE")

      if (not self._check_liftbar_is_homed(context, gcmd)): return False

      if (not self.check_carriage_state(dropoff_tool, 'PRESSED')):
        if gcmd: gcmd.respond_info(
          "Cannot drop tool T%s because it is not attached" % (dropoff_tool.tool_number))
        self.execute_toolchange_pause(context, gcmd)
        return False

      if (not self.check_dock_state(dropoff_tool, 'RELEASED')):
        if gcmd: gcmd.respond_info(
          "Cannot drop tool T%s because it is ALREADY docked." % (dropoff_tool.tool_number))
        self.execute_toolchange_pause(context, gcmd)
        return False

      # Move liftbar to position where change should happen.
      # Consider raising of toolhead which happens in `_dropoff_raise_toolhed`
      cur_x, cur_y, cur_z, cur_e = self.get_current_position()
      raise_toolhead_dist = self.get_macro_var('_TOOLCHANGER_CONFIGURATION', 'raise_toolhead_dist')
      calc_cur_z = cur_z + raise_toolhead_dist
      self.run_gcode_from_command(
         "LIFTBAR_MOVE_TO_CHANGE_POSITION Z=%s DT=%s PT=%s SYNC=0" %
         (calc_cur_z, dropoff_tool.tool_number, pickup_tool.tool_number))

      wait_temperature_delta = self.get_macro_var('_TOOLCHANGER_CONFIGURATION', 'wait_temperature_delta', 0)
      self._set_toolhead_temperature(dropoff_tool, wait_temperature_delta, False, gcmd)
      self._set_toolhead_temperature(pickup_tool, 0, False, gcmd)

      self.save_macro_var('T%s' % dropoff_tool.tool_number, 'color', "''")

      return True

    def dropoff_gcode(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      if (not self._check_liftbar_is_homed(context, gcmd)): return False
      if (not self._drop_raise_toolhead(context, gcmd)): return False
      if (not self._drop_move_to_close_position(context, gcmd)): return False
      if (not self._drop_move_to_park_position(context, gcmd)): return False
      self._drop_change_current(gcmd)
      if (not self._drop_lock_toolhead_on_park_position(context, gcmd)): return False

      # @TODO: it could be inner variable
      self.save_macro_var('PRINT_START', 'tc_no_tool_attached', 1)

      if (not self._drop_move_back_to_safe_positoin(context, gcmd)): return False

      return True

    def _check_liftbar_is_homed(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _check_liftbar_is_homed...")
      if (self.get_macro_var('LIFTBAR_HOME', 'homed', 0) != 1):
        if gcmd: gcmd.respond_info("Liftbar was not homed. Home lieftbar first.")
        self.execute_toolchange_pause(context, gcmd)
        return False

      return True

    def _drop_raise_toolhead(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_raise_toolhead...")

      cur_x, cur_y, cur_z, cur_e = self.get_current_position()
      x_max, y_max, z_max, _ = self.get_axis_maximum()
      # x_min, y_min, z_min, _ = self.get_axis_minimum()

      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      raise_toolhead_dist = cfg['raise_toolhead_dist']
      fast_speed_z = cfg['fast_speed_z']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute

      calc_cur_z = min([cur_z + raise_toolhead_dist, z_max])
      g0_params = {'Z': calc_cur_z, 'F': fast_speed_z}
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", g0_params))

      return True

    def _drop_move_to_close_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_move_to_close_position...")
      cfg               = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      fast_speed_z      = cfg['fast_speed_z']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      dropoff_tool = context['dropoff_tool']
      park_x = dropoff_tool.params['params_park_x']
      park_y = dropoff_tool.params['params_park_y']
      park_z = dropoff_tool.params['params_park_z']

      cur_x, cur_y, cur_z, cur_e = self.get_current_position()

      liftbar_mode = self.get_macro_var('LIFTBAR_HOME', 'mode')
      liftbar_z = self.get_macro_var('LIFTBAR_HOME', 'target_position')
      liftbar_max_z = self.get_macro_var('LIFTBAR_HOME', 'home_pos')
      h_relative_to_t0_nozzle = liftbar_max_z - liftbar_z
      need_raise_toolhead = park_z + h_relative_to_t0_nozzle - cur_z
      z = cur_z + need_raise_toolhead

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute

      if cur_y < safe_y:
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Y': safe_y, 'F': (fast_speed * speed_ratio)
        }))

      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'X': park_x, 'Y': safe_y, 'F': (fast_speed * speed_ratio)
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Z': z, 'F': (fast_speed_z)
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': close_y, 'F': (parking_speed * speed_ratio)
      }))

      self.run_gcode_from_command("LIFTBAR_MOVE SYNC=1")  # sunc/wait for all liftbar moves.

      return True

    def _drop_move_to_park_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_move_to_park_position...")
      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      dropoff_tool = context['dropoff_tool']
      park_x = dropoff_tool.params['params_park_x']
      park_y = dropoff_tool.params['params_park_y']
      park_z = dropoff_tool.params['params_park_z']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (park_y + 5), 'F': (parking_speed * speed_ratio)
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (park_y), 'F': (end_parking_speed * speed_ratio)
      }))

      return True

    def _drop_change_current(self, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_change_current...")
      # @TODO: Load from configuration.
      self.save_macro_var('_TOOLCHANGER_CONFIGURATION', 'run_current_x', 0.875)
      self.save_macro_var('_TOOLCHANGER_CONFIGURATION', 'run_current_y', 0.875)
      self.save_macro_var('_TOOLCHANGER_CONFIGURATION', 'run_current_z', 0.875)

      cfg = self.get_macro_vars('_TOOLCHANGER_CONFIGURATION')
      change_current_x = cfg['change_current_x']
      change_current_y = cfg['change_current_y']
      change_current_z = cfg['change_current_z']

      self.run_gcode_from_command('SET_TMC_CURRENT STEPPER=stepper_x CURRENT=%s' % (change_current_x))
      self.run_gcode_from_command('SET_TMC_CURRENT STEPPER=stepper_y CURRENT=%s' % (change_current_y))

    def _drop_lock_toolhead_on_park_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_lock_toolhead_on_park_position...")
      dropoff_tool = context['dropoff_tool']
      return self._drop_tool_with_dock_check(dropoff_tool, context, gcmd)

    def _drop_tool_with_dock_check(self, tool, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_tool_with_dock_check...")
      x_pos =  tool.params['params_park_x'] - tool.params['params_park_unlock_move']
      # curtime = self.printer.get_reactor().monotonic()

      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      for step in [0, 0.25, 0.5, 0.75, 1, 1.10, 1.20, 1.30, 1.40, 1.50, 1.6, 1.7, 1.8, 1.9, 2.0]:
        if gcmd: gcmd.respond_info("Toolhead T%s checking docksense with step=%s." % (tool.tool_number, step))
        g0_params = {'X': (x_pos - step), 'F': (end_parking_speed * speed_ratio)}
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G1", "G1", g0_params))
        self.run_gcode_from_command("M400")
        if self.check_dock_state(tool, 'PRESSED'):
            if gcmd: gcmd.respond_info("Toolhead T%s docksense was triggered." % (tool.tool_number))

            if tool.tool_number not in self.drop_docksense_corrections:
              self.drop_docksense_corrections[tool.tool_number] = []
            if len(self.drop_docksense_corrections[tool.tool_number]) > 100:
              avg_correction = sum(self.drop_docksense_corrections[tool.tool_number]) / len(self.drop_docksense_corrections[tool.tool_number])
              self.drop_docksense_corrections[tool.tool_number] = []
              self.drop_docksense_corrections[tool.tool_number].append(avg_correction)
            self.drop_docksense_corrections[tool.tool_number].append(step)
            if gcmd:
              gcmd.respond_info("Toolhead T%s average `drop_docksense_corrections` step correction=%s." %
                (tool.tool_number, sum(self.drop_docksense_corrections[tool.tool_number]) / len(self.drop_docksense_corrections[tool.tool_number]))
              )

            return True

      if gcmd:
        gcmd.respond_info(
          "Cannot dock tool=T%s. Docked sensor is not pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `PRESSED`" %
          (tool.tool_number, tool.tool_number)
        )
      self.execute_toolchange_pause(context)
      return False

    def _drop_move_back_to_safe_positoin(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_move_back_to_safe_positoin...")
      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      safe_y_no_tool    = cfg['safe_y_no_tool']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      dropoff_tool = context['dropoff_tool']
      park_x = dropoff_tool.params['params_park_x']
      park_y = dropoff_tool.params['params_park_y']
      park_z = dropoff_tool.params['params_park_z']

      if (not self._drop_tool_with_carriage_check(dropoff_tool, context, gcmd)):
         return False

      # Move to close position & check dock sensor.
      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (close_y), 'F': (parking_speed * 3 * speed_ratio)
      }))
      self.run_gcode_from_command("M400")
      if not self.check_dock_state(dropoff_tool, 'PRESSED'):
        if gcmd: gcmd.respond_info(
           "Cannot dock tool=T%s. Docked sensor is not pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `PRESSED`" %
           (dropoff_tool.tool_number, dropoff_tool.tool_number)
          )
        self.execute_toolchange_pause(context)
        return False

      # Move to safe position
      # @TODO: it could be inner variable
      tc_no_tool_attached = self.get_macro_var('PRINT_START', 'tc_no_tool_attached', 0)
      if tc_no_tool_attached == 1:
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Y': (safe_y_no_tool), 'F': (fast_speed * speed_ratio)
        }))
      else:
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Y': (safe_y), 'F': (fast_speed * speed_ratio)
        }))

      return True

      # _GMOVE_WAIT Y={close_y} F={parking_speed * 3 * speed_ratio}

    def _drop_tool_with_carriage_check(self, tool, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _drop_tool_with_carriage_check...")
      y_pos = tool.params['params_park_y']
      # curtime = self.printer.get_reactor().monotonic()

      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio = cfg['speed_ratio']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      for step in [0, 1, 2, 3]:
        if gcmd: gcmd.respond_info("Toolhead T%s checking carriagesense with step=%s." % (tool.tool_number, step))
        g0_params = {'Y': (y_pos + step), 'F': (end_parking_speed * speed_ratio)}
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G1", "G1", g0_params))
        self.run_gcode_from_command("M400")
        if self.check_carriage_state(tool, 'RELEASED'):
          if gcmd: gcmd.respond_info("Toolhead T%s carriagesense was triggered." % (tool.tool_number))
          return True
        else:
          if not self.check_dock_state(tool, 'PRESSED'):
            if gcmd: gcmd.respond_info(
              "Cannot dock tool=T%s. Docked sensor is not pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `PRESSED`" %
                (tool.tool_number, tool.tool_number)
              )
            self.execute_toolchange_pause(context)
            return False

      if gcmd:
         gcmd.respond_info(
          "Cannot dock tool=T%s. Carraige sensor is not released. Check `QUERY_BUTTON button=carriagesense_t%s` it should be `RELEASED`" %
          (tool.tool_number, tool.tool_number)
        )
      self.execute_toolchange_pause(context)
      return False

    def pickup_gcode(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      if (not self._check_liftbar_is_homed(context, gcmd)): return False

      pickup_tool = context['pickup_tool']

      if (not self.check_carriage_state(pickup_tool, 'RELEASED')):
        if gcmd: gcmd.respond_info("Cannot pick up the tool T%s because it is ALREADY on carriage.") % (pickup_tool.tool_number)
        self.execute_toolchange_pause(context, gcmd)
        return False

      if (not self.check_dock_state(pickup_tool, 'PRESSED')):
        if gcmd: gcmd.respond_info("Cannot pick up the tool T%s because it is not docked.") % (pickup_tool.tool_number)
        self.execute_toolchange_pause(context, gcmd)
        return False

      if (not self._pickup_preheat_tool(context, gcmd)): return False
      if (not self._pickup_move_to_close_position(context, gcmd)): return False
      if (not self._pickup_move_to_park_position(context, gcmd)): return False
      if (not self._pickup_unlock_on_park_position(context, gcmd)): return False
      if (not self._pickup_change_current(gcmd)): return False
      #
      if (not self._pickup_purge_in_place(context, gcmd)): return False
      if (not self._pickup_ramming_on_change(context, gcmd)): return False
      if (not self._pickup_clean_nozzle_in_place(context, gcmd)): return False
      if (not self._pickup_retract_on_change(context, gcmd)): return False
      #
      if (not self._pickup_move_back_to_safe_position(context, gcmd)): return False

      # @TODO: it could be inner variable
      self.save_macro_var('PRINT_START', 'tc_no_tool_attached', 0)

      if (not self._pickup_move_back_to_original_position(context, gcmd)): return False

      return True

    def _pickup_preheat_tool(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_preheat_tool...")
      pickup_tool = context['pickup_tool']
      pickup_tool_name = 'T%s' % pickup_tool.tool_number

      self._set_toolhead_temperature(pickup_tool, 0, False, gcmd)

      return True

    def _pickup_move_to_close_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_move_to_close_position...")
      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      safe_y_no_tool    = cfg['safe_y_no_tool']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      fast_speed_z      = cfg['fast_speed_z']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      pickup_tool = context['pickup_tool']
      park_x = pickup_tool.params['params_park_x']
      park_y = pickup_tool.params['params_park_y']
      park_z = pickup_tool.params['params_park_z']
      unlock_move = pickup_tool.params['params_park_unlock_move']

      cur_x, cur_y, cur_z, cur_e = self.get_current_position()
      x_max, y_max, z_max, _ = self.get_axis_maximum()

      # @TODO: it could be inner variable
      tc_no_tool_attached = self.get_macro_var('PRINT_START', 'tc_no_tool_attached', 0)
      if tc_no_tool_attached:
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Y': (safe_y_no_tool), 'F': (fast_speed * speed_ratio)
        }))
      else:
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Y': (safe_y), 'F': (fast_speed * speed_ratio)
        }))

      liftbar_z = self.get_macro_var('LIFTBAR_HOME', 'target_position', 0)
      liftbar_max_z = self.get_macro_var('LIFTBAR_HOME', 'home_pos')
      h_relative_to_t0_nozzle = liftbar_max_z - liftbar_z
      need_raise_toolhead = park_z + h_relative_to_t0_nozzle - cur_z
      calculated_z_pos = cur_z + need_raise_toolhead

      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'X': park_x - unlock_move, 'F': fast_speed * speed_ratio
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Z': calculated_z_pos, 'F': fast_speed_z
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': close_y, 'F': fast_speed * speed_ratio
      }))

      self.run_gcode_from_command('LIFTBAR_MOVE SYNC=1')
      return True

    def _pickup_move_to_park_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_move_to_park_position...")
      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      safe_y_no_tool    = cfg['safe_y_no_tool']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      fast_speed_z      = cfg['fast_speed_z']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      pickup_tool = context['pickup_tool']
      park_x = pickup_tool.params['params_park_x']
      park_y = pickup_tool.params['params_park_y']
      park_z = pickup_tool.params['params_park_z']

      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (park_y + 20), 'F': (parking_speed * 2 * speed_ratio)
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (park_y), 'F': (parking_speed * speed_ratio)
      }))

      return True

    def _pickup_unlock_on_park_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_unlock_on_park_position...")
      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      safe_y_no_tool    = cfg['safe_y_no_tool']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      fast_speed_z      = cfg['fast_speed_z']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      pickup_tool = context['pickup_tool']
      park_x = pickup_tool.params['params_park_x']
      park_y = pickup_tool.params['params_park_y']
      park_z = pickup_tool.params['params_park_z']

      # Check carriage is attached

      self.run_gcode_from_command("M400")
      if (not self.check_carriage_state(pickup_tool, 'PRESSED')):
        # Cannot dock tool=T%s. Docked sensor is not pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `PRESSED`
        # Cannot dock tool=T%s. Carraige sensor is not released. Check `QUERY_BUTTON button=carriagesense_t%s` it should be `RELEASED`
        if gcmd: gcmd.respond_info(
           "Cannot pick up tool=T%s. Carraige sensor is still released. Check `QUERY_BUTTON button=carriagesense_t%s` it should be `PRESSED`" %
           (pickup_tool.tool_number, pickup_tool.tool_number))
        self.execute_toolchange_pause(context, gcmd)
        return False

      # Check dock is released after short moves.
      if (not self._pickup_tool_with_dock_check(pickup_tool, context, gcmd)):
        # if gcmd: gcmd.respond_info('Cannot pick up tool=T%s. Tool is still docked.' % (pickup_tool.tool_number))
        self.execute_toolchange_pause(context, gcmd)
        return False

      # Move to park position and check dock is still released.
      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'X': (park_x), 'F': (end_parking_speed * speed_ratio)
      }))
      self.run_gcode_from_command("M400")
      if (not self.check_dock_state(pickup_tool, 'RELEASED')):
        # Cannot dock tool=T%s. Docked sensor is not pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `PRESSED`
        # Cannot dock tool=T%s. Carraige sensor is not released. Check `QUERY_BUTTON button=carriagesense_t%s` it should be `RELEASED`
        if gcmd: gcmd.respond_info(
           "Cannot pick up tool=T%s. Docked sensor is still pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `RELEASED`" %
           (pickup_tool.tool_number, pickup_tool.tool_number))
        self.execute_toolchange_pause(context, gcmd)
        return False

      if (not self._pickup_tool_with_carriage_check(pickup_tool, context, gcmd)):
        # if gcmd: gcmd.respond_info('Cannot pick up tool=T%s. Carriage is not attached properly.' % (pickup_tool.tool_number))
        self.execute_toolchange_pause(context, gcmd)
        return False

      return True

    def _pickup_tool_with_dock_check(self, tool, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_tool_with_dock_check...")
      park_x = tool.params['params_park_x']
      park_y = tool.params['params_park_y']
      park_z = tool.params['params_park_z']

      cur_x, cur_y, cur_z, cur_e = self.get_current_position()

      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      for step in [2.0, 2.25, 2.5, 2.75, 3.0]:
        if gcmd: gcmd.respond_info("Toolhead T%s checking docksense with step=%s." % (tool.tool_number, step))
        g0_params = {'X': (cur_x + step), 'F': (end_parking_speed * speed_ratio)}
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G1", "G1", g0_params))
        self.run_gcode_from_command("M400")
        if self.check_dock_state(tool, 'RELEASED'):
          if gcmd: gcmd.respond_info("Toolhead T%s docksense was released." % (tool.tool_number))

          if tool.tool_number not in self.pick_docksense_corrections:
            self.pick_docksense_corrections[tool.tool_number] = []
          if len(self.pick_docksense_corrections[tool.tool_number]) > 100:
            avg_correction = sum(self.pick_docksense_corrections[tool.tool_number]) / len(self.pick_docksense_corrections[tool.tool_number])
            self.pick_docksense_corrections[tool.tool_number] = []
            self.pick_docksense_corrections[tool.tool_number].append(avg_correction)
          self.pick_docksense_corrections[tool.tool_number].append(step)
          if gcmd:
            gcmd.respond_info("Toolhead T%s average `pick_docksense_corrections` step correction=%s." %
              (tool.tool_number, sum(self.pick_docksense_corrections[tool.tool_number]) / len(self.pick_docksense_corrections[tool.tool_number]))
            )

          return True

      if gcmd:
        gcmd.respond_info(
          "Cannot pick up tool=T%s. Docked sensor is still pressed. Check `QUERY_BUTTON button=docksense_t%s` it should be `RELEASED`" %
          (tool.tool_number, tool.tool_number)
        )
      self.execute_toolchange_pause(context)
      return False

    def _pickup_tool_with_carriage_check(self, tool, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_tool_with_carriage_check...")
      park_x = tool.params['params_park_x']
      park_y = tool.params['params_park_y']
      park_z = tool.params['params_park_z']

      cur_x, cur_y, cur_z, cur_e = self.get_current_position()

      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio = cfg['speed_ratio']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      for step in [5]:
        if gcmd: gcmd.respond_info("Toolhead T%s checking carriagesense with step=%s." % (tool.tool_number, step))
        g0_params = {'Y': (cur_y + step), 'F': (end_parking_speed * speed_ratio)}
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G1", "G1", g0_params))
        self.run_gcode_from_command("M400")
        if self.check_carriage_state(tool, 'RELEASED'):
          if gcmd: gcmd.respond_info(
            "Cannot pick up tool=T%s. Carriage sensor is not pressed. Check `QUERY_BUTTON button=carriagesense_t%s` it should be `PRESSED`" %
              (tool.tool_number, tool.tool_number)
            )
          self.execute_toolchange_pause(context)
          return False

      return True

    def _pickup_change_current(self, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_change_current...")
      cfg = self.get_macro_vars('_TOOLCHANGER_CONFIGURATION')
      change_current_x = cfg['run_current_x']
      change_current_y = cfg['run_current_y']
      change_current_z = cfg['run_current_z']

      self.run_gcode_from_command('SET_TMC_CURRENT STEPPER=stepper_x CURRENT=%s' % (change_current_x))
      self.run_gcode_from_command('SET_TMC_CURRENT STEPPER=stepper_y CURRENT=%s' % (change_current_y))
      return True

    def _pickup_move_back_to_safe_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_move_back_to_safe_position...")

      # if (not self._pickup_purge_in_place(context, gcmd)): return False
      # if (not self._pickup_ramming_on_change(context, gcmd)): return False
      # if (not self._pickup_clean_nozzle_in_place(context, gcmd)): return False
      # if (not self._pickup_retract_on_change(context, gcmd)): return False

      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      safe_y_no_tool    = cfg['safe_y_no_tool']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (close_y), 'F': (parking_speed * speed_ratio)
      }))
      self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
        'Y': (safe_y), 'F': (fast_speed * speed_ratio)
      }))

      liftbar_mode = self.get_macro_var('LIFTBAR_HOME', 'mode')
      if (liftbar_mode != 1):
        restore_position = context['restore_position']
        command_params = " Z=%s" % restore_position['Z'] if 'Z' in restore_position else ""
        self.run_gcode_from_command("LIFTBAR_LAYER_CHANGE %s", command_params)
        if gcmd:
          gcmd.respond_info("_pickup_move_back_to_safe_position: LIFTBAR_LAYER_CHANGE %s", command_params)

      return True

    def _pickup_purge_in_place(self, context, gcmd = None):

      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_purge_in_place...")
      return True

    def _pickup_ramming_on_change(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_ramming_on_change...")
      return True

    def _pickup_clean_nozzle_in_place(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_clean_nozzle_in_place...")
      return True

    def _pickup_retract_on_change(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_retract_on_change...")
      if (context is None):
        return True

      pickup_tool = context['pickup_tool']
      volume = pickup_tool.params['params_retract_volume_on_change']
      speed = pickup_tool.params['params_retract_speed_on_change']
      is_primed = self.get_macro_var('PRINT_START', 'is_primed', 0)

      if is_primed and volume > 0:
        purge_temp_min = self.get_macro_var('_clean_nozzle_varables', 'purge_temp_min', 200)
        temp, target_temp = self.get_tool_temps(pickup_tool)
        if temp >= purge_temp_min:
          self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
            'E': (-1 * volume), 'F': speed
          }))
          if gcmd: gcmd.respond_info("Retracted %s on T%s" % (volume, pickup_tool.tool_number))

      return True

    def _pickup_detract_on_change(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_detract_on_change...")
      if (context is None):
        return True

      pickup_tool = context['pickup_tool']
      volume = pickup_tool.params['params_detract_volume_on_change']
      speed = pickup_tool.params['params_detract_speed_on_change']
      is_primed = self.get_macro_var('PRINT_START', 'is_primed', 0)

      if is_primed and volume > 0:
        purge_temp_min = self.get_macro_var('_clean_nozzle_varables', 'purge_temp_min', 200)
        temp, target_temp = self.get_tool_temps(pickup_tool)
        if temp >= purge_temp_min:
          self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
            'E': volume, 'F': speed
          }))
          if gcmd: gcmd.respond_info("Detracted %s on T%s" % (volume, pickup_tool.tool_number))

      return True

    def _pickup_move_back_to_original_position(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling _pickup_move_back_to_original_position...")
      cfg = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      safe_y_no_tool    = cfg['safe_y_no_tool']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      fast_speed_z      = cfg['fast_speed_z']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      restore_position = context['restore_position']

      self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute

      if ('Z' in restore_position):
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Z': (restore_position['Z'] + 5), 'F': (fast_speed_z)
        }))

      if ('X' in restore_position) and ('Y' in restore_position):
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'X': restore_position['X'], 'Y': restore_position['Y'], 'F': (fast_speed * speed_ratio)
        }))
      else:
        if ('Y' in restore_position):
          self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
            'Y': restore_position['Y'], 'F': (fast_speed * speed_ratio)
          }))
        if ('X' in restore_position):
          self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
            'X': restore_position['X'], 'F': (fast_speed * speed_ratio)
          }))

      if ('Z' in restore_position):
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Z': (restore_position['Z']), 'F': (fast_speed_z)
        }))


      return True

    def after_change(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling after_change...")
      extra_context = {}

      if (context is not None):
        dropoff_tool = context['dropoff_tool'] if 'dropoff_tool' in context else None
        pickup_tool = context['pickup_tool'] if 'pickup_tool' in context else None

        if dropoff_tool is not None:
          self.save_macro_var('T%s' % dropoff_tool.tool_number, 'color', "''")
          self.run_gcode_from_command("SET_LED_EFFECT EFFECT=T%s_panel_idle REPLACE=1" % dropoff_tool.tool_number)

        if pickup_tool is not None:
          self.save_macro_var('T%s' % pickup_tool.tool_number, 'color', "'c44'")
          self.run_gcode_from_command("SET_LED_EFFECT EFFECT=T%s_default_light REPLACE=1" % pickup_tool.tool_number)

        # if pickup_tool is not None:
        #   if (pickup_tool.params['params_input_shaper_freq_x'] and pickup_tool.params['params_input_shaper_freq_y']):
        #     self.run_gcode_from_command(
        #         "SET_INPUT_SHAPER SHAPER_FREQ_X=%s SHAPER_FREQ_Y=%s" %
        #         (pickup_tool.params['params_input_shaper_freq_x'],
        #         pickup_tool.params['params_input_shaper_freq_y']))

        if dropoff_tool is not None:
          self.run_gcode_from_command("LIFTBAR_LAYER_CHANGE")

        extra_context = {
          'dropoff_tool': dropoff_tool.name if dropoff_tool else None,
          'pickup_tool': pickup_tool.name if pickup_tool else None,
          'restore_position': context['restore_position'] if 'restore_position' in context else {}
        }

      self.last_dropoff_tool = None
      self.last_pickup_tool = None
      self.last_restore_position = None

      # self.run_gcode('after_change_gcode', self.after_change_gcode, extra_context)
      return True

    def finalize_after_change(self, context, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling finalize_after_change...")

      if (context is not None):
        dropoff_tool = context['dropoff_tool'] if 'dropoff_tool' in context else None
        pickup_tool = context['pickup_tool'] if 'pickup_tool' in context else None

        params_feedrate = self.get_macro_var('PRINT_START', 'params_feedrate', None)
        if params_feedrate and params_feedrate >= 0:
          self.run_gcode_from_command("M220 S%s" % params_feedrate)

        if (not self._pickup_detract_on_change(context, gcmd)): return False

      return True

    def execute_toolchange_pause(self, context = None, gcmd = None):
      if self.status == STATUS_PAUSED:
        return False

      if gcmd: gcmd.respond_info("Calling execute_toolchange_pause...")

      self.status = STATUS_PAUSED
      # @TODO: remove it later. Legacy
      self.save_macro_var('_LTC_PAUSE', 'is_ltc_paused', 1)

      self.run_gcode_from_command("M400")
      # self._pickup_change_current(gcmd)

      # Store details to be able to manitulate in after pause.
      if self.last_dropoff_tool is None:
         if (context is not None and 'dropoff_tool' in context):
            self.last_dropoff_tool = context['dropoff_tool']
      if self.last_pickup_tool is None:
         if (context is not None and 'pickup_tool' in context):
            self.last_pickup_tool = context['pickup_tool']
      if self.last_restore_position is None:
        cur_x, cur_y, cur_z, cur_e = self.get_current_position()
        if (context is not None and 'restore_position' in context):
          self.last_restore_position = context['restore_position']
        else:
          self.last_restore_position = {'X': cur_x, 'Y': cur_y, 'Z': cur_z}

      # @TODO: it could be inner variable
      self.save_macro_var('PRINT_START', 'tc_no_tool_attached', 0)

      if self.is_in_priniting_state():
        self.run_gcode_from_command("SEND_MOBI_MESSAGE MSG='!!! Print was paused on toolhaead change and needs your attention.'")
        self.run_gcode_from_command("PAUSE MOVE=0 MODE=1")

      self.toolchange_pause_print_details(gcmd)

      return True

    def toolchange_pause_print_details(self, gcmd):
      if self.status == STATUS_PAUSED:
        dropoff_tool_number = self.last_dropoff_tool.tool_number if self.last_dropoff_tool else 'UNDEFINED'
        pickup_tool_number = self.last_pickup_tool.tool_number if self.last_pickup_tool else 'UNDEFINED'
        last_restore_position = self.last_restore_position if self.last_restore_position else {'X': 'UNDEFINED', 'Y': 'UNDEFINED', 'Z': 'UNDEFINED', }
        if gcmd: gcmd.respond_info(
          "Error while tool changing from T%s to T%s. \n"
          "Please attach manually the tool `T%s` and \n"
          "call `LTC_PAUSE_RESOLVE T=%s` macro after all done. \n"
          "To check the sensors state, please use: \n"
          "`QUERY_BUTTON button=carriagesense_t%s` \n"
          "`QUERY_BUTTON button=docksense_t%s` \n"
          "Restore position: X=%s Y=%s Z=%s" %
          (dropoff_tool_number, pickup_tool_number,
          pickup_tool_number,
          pickup_tool_number,
          pickup_tool_number, pickup_tool_number,
          last_restore_position['X'] if 'X' in last_restore_position else 'UNDEFINED',
          last_restore_position['Y'] if 'Y' in last_restore_position else 'UNDEFINED',
          last_restore_position['Z'] if 'Z' in last_restore_position else 'UNDEFINED')
        )
      else :
        if gcmd: gcmd.respond_info("The printer is not in pause state.")

    def execute_toolchange_pause_resolve(self, tool, gcmd = None):
      if self.status != STATUS_PAUSED:
        return False

      if not self.check_carriage_state(tool, 'PRESSED'):
        if gcmd: gcmd.respond_info("Tool T%s is not on the carriage." % (tool.tool_number))
        return False

      if not self.check_dock_state(tool, 'RELEASED'):
        if gcmd: gcmd.respond_info("Tool T%s is docked, please attach it to carriage." % (tool.tool_number))
        return False

      # Initialise tool
      try:
        self.initialize(tool)
      except Exception as e:
        if gcmd: gcmd.respond_info(
           "Cannot initialize tool T%s. Raised error: %s" %
           (tool.tool_number, e))
        return False

      # Apply offsets for new tool
      self._set_tool_gcode_offset(tool)
      # Preheat tool
      self._set_toolhead_temperature(tool, 0, True, gcmd)
      # change color in UI
      for tool_number in self.printer.lookup_object('toolchanger').tool_numbers:
        self.save_macro_var('T%s' % tool_number, 'color', "''")
      # change color in UI for active tool
      self.save_macro_var('T%s' % tool.tool_number, 'color', "'c44'")

      # @TODO: remove it later. Legacy
      self.save_macro_var('_LTC_PAUSE', 'is_ltc_paused', 1)
      # set TMC current
      self._pickup_change_current(gcmd)

      cfg               = self.get_macro_vars("_TOOLCHANGER_CONFIGURATION")
      safe_y            = cfg['safe_y']
      close_y           = cfg['close_y']
      fast_speed        = cfg['fast_speed']
      fast_speed_z      = cfg['fast_speed_z']
      parking_speed     = cfg['parking_speed']
      end_parking_speed = cfg['end_parking_speed']
      speed_ratio       = cfg['speed_ratio']

      cur_x, cur_y, cur_z = self.get_current_position()
      if cur_y < safe_y:
        self.gcode_move.cmd_G90(self.gcode.create_gcode_command("G90", "G90", {})) # go absolute
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command("G0", "G0", {
          'Y': safe_y, 'F': (fast_speed * speed_ratio)
        }))

      if self.is_in_paused_state():
        # @TODO: Clear and Prime tool after long pause???
        self._pickup_move_back_to_original_position({
          'dropoff_tool': self.last_dropoff_tool,
          'pickup_tool': tool,
          'restore_position': self.last_restore_position
        }, gcmd)
        self.run_gcode_from_command("LIFTBAR_LAYER_CHANGE")
        self.run_gcode_from_command("M400")
        self.run_gcode_from_command("RESTORE_GCODE_STATE NAME=_toolchange_state MOVE=0")
        self.run_gcode_from_command("SAVE_GCODE_STATE NAME=PAUSE_STATE")
        self.save_macro_var('RESUME', 'pause_mode', 1)
        self.run_gcode_from_command("RESUME")

      self.last_dropoff_tool = None
      self.last_pickup_tool = None
      self.last_restore_position = None

      # @TODO: remove it later. Legacy
      self.save_macro_var('_LTC_PAUSE', 'restore_position_x', -1)
      self.save_macro_var('_LTC_PAUSE', 'restore_position_y', -1)
      self.save_macro_var('_LTC_PAUSE', 'restore_position_z', -1)
      self.save_macro_var('_LTC_PAUSE', 'restore_position_saved', 0)

      # @TODO: remove it later. Legacy
      self.save_macro_var('_LTC_PAUSE', 'dropoff_tool_number', -1)
      self.save_macro_var('_LTC_PAUSE', 'pickup_tool_number', -1)

def get_params_dict(config):
    result = {}
    for option in config.get_prefix_options('params_'):
        try:
            result[option] = ast.literal_eval(config.get(option))
        except ValueError as e:
            raise config.error(
                "Option '%s' in section '%s' is not a valid literal" % (
                    option, config.get_name()))
    return result

def load_config(config):
    return Toolchanger(config)

def load_config_prefix(config):
    return Toolchanger(config)
