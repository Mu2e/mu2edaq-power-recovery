# Mu2e Power Outage Recovery Tools

This set of tools is designed to allow the Mu2e experiment to recover from a planned or unplanned power outage.  The project will provide a set of tools to automate the process of assessing the state of the data acquistion system, turn it on, turn it off, verify all its components, restore any types of connectivity it needs and generate reports.

These tools should be able to be initiated from a machine outside of the DAQ networks.  Assume that the administrator running these checks has or can obtain:

* a value kerberos principal that can be used to access the cluster resources
* a value token for access to the vault infrastructure
* write access to a location for hosting the report wepages (this can be local or can be a remote upload address)

Secrets are held in a hashicorp vault:
https://ssivault.fnal.gov:8200
td/scd/experiments/mu2e/

The ipmi secrets are in:
td/scd/experiments/mu2e/ipmi

# DAQ Computing Centers

The mu2e daq has 3 separate computing centers that need to be checked.  They are located in the MC-1 building, the MC-2 building, and the Helen Edwards Enginering Center (heerc) building.  They should be referred to as MC-1, MC-2, and Teststand.

## Node Names

The compute nodes for each building can be determined from the scripts in the mu2edaq-operations repository.  The node_list.py scripts should provide this information.

## Network Segments

The computing nodes have connections to upto three different local area networks.  The networks are:

* "lab" (131.225.245.0/24)
* "data" (10.226.9.0/24)
* "ipmi" (192.168.157.0/24)

In the heerc building the networks are:
* "lab" (131.225.237.0/24)
* "data" (10.226.9.0/24)
* "ipmi" (192.168.150.0/24)

In the mc-1 building the networks are:
* "lab" (131.225.246.0/24)
* There is no data network in mc-1
* 

# Current State report

There needs to be a tool that can determine the current state of machines in each of these areas.  It should generate both an on screen report of the state of the machines and a webpage with tables of the states of the machines in each area and by each class.

Rerunning the report should update the webpage.

When running the tools the first thing they should do is check for updates from the github repo and update if needed and rebuild if needed.

They should then print information about the version being run.

The following items should be *checked* when running the first phase of the state report, no corrective actions should be taken during this phase:

* Check that the gateway/firewall machines are responding
* Check that login to the gateway/firewall machines operates properly
    - This will require loging in with a kerberos ticket that has root access
    - The tools should allow for the designation of a set of kerberos principals to use for access (general and root)
    - The scripts should prompt the user if they need to get a password for a kerberos principal
* Check the list of disk mounts on the gateway machines to ensure that they match the normal configuration
* From the gateway machines check that other machines are responding
* From the gateway machines check for the power status of other machines
* From the gateway machines attempt to login to other machines as the root user and verify the disk mounts and status of network interfaces
* Perform other health checks on the different machines to determine their current state.  Health checks should include power, networking, disks and any other low level checks.

After the initial checkout phase the first set of webpages should be generated.  These are the "intitial state" pages.

In phase 2 we are going to systemmatically turn on different machines in a very specific order.

To do this:

* IPMI commands should be able to be issued from gateway01 or gateway02
* The first machine that needs to turn on after the gateways is mu2e-mgr-01.fnal.gov
* Ensure this machine is turned on (use ipmi to turn it on or off)
* Ensure that this machine can be logged into as root.
* Ensure that all the disks on this machine come up and are not reporting errors
* Ensure that logins as the user mu2edaq and mu2eshift work to mu2e-mgr-01.fnal.gov
* Ensure that network interfaces are up on mu2e-mgr-01.fnal.gov

After mu2e-mgr-01.fnal.gov is verified to be up and configured properly:

* Do the same set of power on and checks for mu2e-dl-01.fnal.gov and mu2e-dl-02.fnal.gov
* Then do the same for mu2e-dcs-01 and mu2e-dcs-02
* Ensure that they additionally mount disk areas correctly from mu2e-mgr-01.fnal.gov

Next ensure that mu2e-cfo-01 comes up and is properly configured include power, networking, disk, attached devices and other low level checks.

After that bring up
* mu2e-crv-01.fnal.gov
* mu2e-trk-XX.fnal.gov nodes 1 through 14
* mu2e-calo-XX.fnal.gov nodes 1-11
* mu2e-stm-01 and mu2e-stm-02

Perform health checks on each machine.  Then generate the second set of webpages based on the results.  These are the "power on" pages.

In Phase 3 perform additional checks of the network connectivity between nodes to ensure that there are no network issues between machines on the various network interfaces.

Make the third set of webpages based on these tests.

In phase 4 generate a detailed report of all the steps that were taken.  We will want to be able to post this to our electronics logbook (and I have code that will do that, see the ecl-client repo for these tools.

