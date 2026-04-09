#!/usr/bin/env python3

import datetime, asyncio, time, sys

from givenergy_modbus.client.client import Client
from givenergy_modbus.model.plant import Plant, Inverter
from palm_utils import GivEnergyObjLocal

#
# async def runtest():
#     client = Client(host="192.168.1.202", port="8899")
#     await client.connect()
#
#
#     # note - importing givenergy_modbus.client.commands is now deprecated
#     commands = client.commands
#
#     # This command works
#     # await client.execute(commands.set_charge_target(100),1,1)
#
#
#     # await client.exec(commands.enable_charge_target(80))
#     # # set a charging slot from 00:30 to 04:30
#     # await client.execute(commands.set_charge_slot_1((datetime.time(hour=0, minute=30), datetime.time(hour=4, minute=30))))
#
#
#     # # set the inverter to charge when there's excess, and discharge otherwise. it will also respect charging slots.
#     # await client.exec(commands.set_mode_dynamic())
#
#     await client.refresh_plant(full_refresh=True)
#
#     p = client.plant
#     inverter = p.inverter
#
#     # Test that data has been correctly retreived from inverter
#     assert inverter.serial_number == 'CE2146G203'
#
#     print(inverter)
#
#     print("######")
#
#     print("Battery SoC", inverter.battery_percent)
#     print("Charge Start", inverter.charge_slot_1_start)
#     print("Charge End", inverter.charge_slot_1_end)
#     print("Discharge Start", inverter.discharge_slot_1_start)
#     print("Discharge End", inverter.discharge_slot_1_end)
#
#     print("AC Voltage", inverter.v_ac1)
#     print("AC Frequency", inverter.f_ac1)
#     print("PV Power", inverter.p_pv1)
#     print("Inverter Power", inverter.p_inverter_out)
#     print("Grid Power", inverter.p_grid_out)
#     print("Load Power", inverter.p_load_demand)
#
#     print("######")
#
#
#     # assert inverter.model == 3
#     # assert inverter.v_pv1 == 1.4  # V
#     # assert inverter.e_battery_discharge_day == 8.1  # kWh
#     # assert inverter.enable_charge_target
#
#     # b0 = p.batteries[0]
#     # print(b0)
#
#     # assert b0.serial_number == 'BG1234G567'
#     # assert b0.v_battery_cell_01 == 3.117
#
#     await client.execute(commands._set_helper("charge_slot_1_start",2330),1,1)
#     await client.execute(commands._set_helper("charge_slot_1_end",530),1,1)
#
#     # await client.execute(commands._set_helper("charge_slot_1_start",0),1,1)
#     # await client.execute(commands._set_helper("charge_slot_1_end",2330),1,1)
#     await client.execute(commands.set_enable_charge(True),1,1)
#     await client.execute(commands.set_charge_target(100),1,1)


if __name__ == '__main__':

    inverter: GivEnergyObjLocal = GivEnergyObjLocal()


    # inverter.get_latest_data()
    # print(inverter.__dict__)
    # time.sleep(5)

    # inverter.set_mode("charge_now")
    inverter.set_mode("charge_now")
    time.sleep(30)


    inverter.get_latest_data()
    print(inverter.__dict__)
    time.sleep(5)

    inverter.set_mode("discharge_now")
    time.sleep(30)

    inverter.get_latest_data()
    print(inverter.__dict__)
    time.sleep(5)

    inverter.set_mode("pause")
    time.sleep(30)

    inverter.get_latest_data()
    print(inverter.__dict__)
    time.sleep(5)

    inverter.set_mode("play")
    time.sleep(30)

    inverter.get_latest_data()
    print(inverter.__dict__)
    time.sleep(5)
    #
    # counter = 0
    # while counter < 100000:
    #     inverter.get_latest_data()
    #     print("Counter:", counter)
    #     print(inverter.__dict__)
    #     sys.stdout.flush()
    #     time.sleep(5)
    #     counter += 1
    #
    # asyncio.run(runtest())
