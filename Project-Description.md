# Mu2e Power Outage Recovery Tools

This set of tools is designed to allow the Mu2e experiment to recover from a planned or unplanned power outage.  The project will provide a set of tools to automate the process of assessing the state of the data acquistion system, turn it on, turn it off, verify all its components, restore any types of connectivity it needs and generate reports.

# DAQ Computing Centers

The mu2e daq has 3 separate computing centers that need to be checked.  They are located in the MC-1 building, the MC-2 building, and the Helen Edwards Enginering Center (heerc) building.  They should be referred to as MC-1, MC-2, and Teststand.

## Node Names

The compute nodes for each building can be determined from the scripts in the mu2edaq-operations repository.  The node_list.py scripts should provide this information.

## Network Segments

The computing nodes have connections to upto three different local area networks.  The networks are:

* "lab" (131.225.245.0/24)
* "data" (10.226.9.0/24)
* "ipmi" ()