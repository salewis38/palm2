PALM2 is a re-factoring of the original PALM code to run asynchronously with local inverter control. It also contains other changes, as much has moved on since the original.

The original PALM served two main purposes:  PV export reduction through regulation of overnight battery charge and control of various time-agnostic daytime loads; and automatic data upload to PVOutput.org. The prediction algorithm for export reduction was subsequently added to HA as a precursor to Predbat. Export payments, rather than deemed/no export, are now prevalent; one of the original purposes of PALM has now served its time. The reliance on the long-term existence of external data sources for the prediction algorithm, especially the GivENergy API, is a further consideration.

**PALM 2 has the following objectives:**
* Simple, stand-alone, robust code that can be easily installed on any Linux platform, such as a Raspberry Pi or router to extract data from and enable local control of a local GivEnergy battery system
* Automatic upload of consumption data and other parameters to PVOutput.org for visualisation and long-term analysis
* Monitoring of EV charging (agnostic of EVSE type via a Shelly Energy Monitor), enabling appropriate battery control and control of other loads via Shelly switches
* Controlled battery export during summer evenings
* Enabling other battery control use cases through a set of library calls
* 

**INSTALLATION INSTRUCTIONS FOR LINUX-BASED SYSTEMS, INCLUDING HOW TO RUN AS A SERVICE ON RASPBERRY PI**

Create local directories:
$ mkdir /home/pi/palm
$ mkdir /home/pi/logs

Download all files to /home/pi/palm/
$ cd /home/pi/palm
$ wget github.com/salewis38/palm/archive/heads/main.zip
$ unzip main.zip
$ cp -rp palm-heads-main/* ./

Edit palm_settings.py with your system details, etc
$ nano palm_settings.py

Run palm.py, initially in test mode with the command:
$ ./palm.py -t

To run as a persistent service, execute the following commands:
$ sudo cp palm.service /lib/systemd/system
$ sudo systemctl start palm.service
$ sudo systemctl enable palm.service

This will run palm.py in the background and save date-coded logfiles to /home/pi/logs

Enjoy!

