# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re
from typing import Any, Dict, Tuple

from assertpy import assert_that, fail

from lisa import (
    Environment,
    Logger,
    Node,
    SkippedException,
    TestCaseMetadata,
    TestSuite,
    TestSuiteMetadata,
    UnsupportedDistroException,
    schema,
    search_space,
)
from lisa.features import Gpu, Infiniband, IsolatedResource, NetworkInterface, Sriov
from lisa.testsuite import simple_requirement
from lisa.tools import Echo, Git, Ip, Kill, Lsmod, Make, Modprobe, Service, Timeout
from lisa.util.constants import SIGINT
from microsoft.testsuites.dpdk.common import DPDK_STABLE_GIT_REPO
from microsoft.testsuites.dpdk.dpdknffgo import DpdkNffGo
from microsoft.testsuites.dpdk.dpdkovs import DpdkOvs
from microsoft.testsuites.dpdk.dpdkutil import (
    UIO_HV_GENERIC_SYSFS_PATH,
    UnsupportedPackageVersionException,
    check_send_receive_compatibility,
    enable_uio_hv_generic_for_nic,
    generate_send_receive_run_info,
    init_hugepages,
    init_nodes_concurrent,
    initialize_node_resources,
    run_testpmd_concurrent,
    verify_dpdk_build,
    verify_dpdk_send_receive,
    verify_dpdk_send_receive_multi_txrx_queue,
)
from microsoft.testsuites.dpdk.dpdkvpp import DpdkVpp

VDEV_TYPE = "net_vdev_netvsc"
MAX_RING_PING_LIMIT_NS = 200000
DPDK_VF_REMOVAL_MAX_TEST_TIME = 60 * 10


@TestSuiteMetadata(
    area="dpdk",
    category="functional",
    description="""
    This test suite check DPDK functionality
    """,
)
class Dpdk(TestSuite):
    # regex for parsing ring ping output for the final line,
    # grabbing the max latency of 99.999% of data in nanoseconds.
    # ex: percentile 99.999 = 12302
    _ring_ping_percentile_regex = re.compile(r"percentile 99.990 = ([0-9]+)")

    @TestCaseMetadata(
        description="""
            netvsc direct pmd version.
            This test case checks DPDK can be built and installed correctly.
            Prerequisites, accelerated networking must be enabled.
            The VM should have at least two network interfaces,
             with one interface for management.
            More details refer https://docs.microsoft.com/en-us/azure/virtual-network/setup-dpdk#prerequisites # noqa: E501
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
        ),
    )
    def verify_dpdk_build_netvsc(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        verify_dpdk_build(node, log, variables, "netvsc")

    @TestCaseMetadata(
        description="""
            failsafe (azure default, recommended) version.
            This test case checks DPDK can be built and installed correctly.
            Prerequisites, accelerated networking must be enabled.
            The VM should have at least two network interfaces,
            with one interface for management.
            More details: https://docs.microsoft.com/en-us/azure/virtual-network/setup-dpdk#prerequisites # noqa: E501
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
        ),
    )
    def verify_dpdk_build_failsafe(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        verify_dpdk_build(node, log, variables, "failsafe")

    @TestCaseMetadata(
        description="""
           Install and run OVS+DPDK functional tests
        """,
        priority=4,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
            disk=schema.DiskOptionSettings(
                data_disk_count=search_space.IntRange(min=1),
                data_disk_size=search_space.IntRange(min=32),
            ),
        ),
    )
    def verify_dpdk_ovs(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        # initialize DPDK first, OVS requires it built from source before configuring.
        self._force_dpdk_default_source(variables)
        test_kit = initialize_node_resources(node, log, variables, "failsafe")

        # checkout OpenVirtualSwitch
        ovs = node.tools[DpdkOvs]

        # provide ovs build with DPDK tool info and build
        ovs.build_with_dpdk(test_kit.testpmd)

        # enable hugepages needed for dpdk EAL
        init_hugepages(node)

        try:
            # run OVS tests, providing OVS with the NIC info needed for DPDK init
            ovs.setup_ovs(node.nics.get_nic_by_index().pci_slot)

            # validate if OVS was able to initialize DPDK
            node.execute(
                "ovs-vsctl get Open_vSwitch . dpdk_initialized",
                sudo=True,
                expected_exit_code=0,
                expected_exit_code_failure_message=(
                    "OVS repoted that DPDK EAL failed to initialize."
                ),
            )
        finally:
            ovs.stop_ovs()

    @TestCaseMetadata(
        description="""
           Install and run ci test for NFF-Go on ubuntu
        """,
        priority=4,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_nff_go(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        try:
            nff_go = node.tools[DpdkNffGo]
        except UnsupportedDistroException as err:
            raise SkippedException(err)

        # hugepages needed for dpdk tests
        init_hugepages(node)
        # run the nff-go tests
        nff_go.run_test()

    @TestCaseMetadata(
        description="""
           Build and run DPDK multiprocess client/server sample application.
           Requires 3 nics since client/server needs two ports + 1 nic for LISA
        """,
        priority=4,
        requirement=simple_requirement(
            min_nic_count=3,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_multiprocess(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        # multiprocess test requires dpdk source.
        self._force_dpdk_default_source(variables)
        kill = node.tools[Kill]
        pmd = "failsafe"
        server_app_name = "dpdk-mp_server"
        client_app_name = "dpdk-mp_client"
        # initialize DPDK with sample applications selected for build
        test_kit = initialize_node_resources(
            node,
            log,
            variables,
            pmd,
            sample_apps=[
                "multi_process/client_server_mp/mp_server",
                "multi_process/client_server_mp/mp_client",
            ],
        )

        if test_kit.testpmd.is_connect_x3:
            raise SkippedException(
                "Unsupported Hardware: ConnectX3 does not support secondary process RX"
            )

        # enable hugepages needed for dpdk EAL
        init_hugepages(node)

        # setup and run mp_server application
        examples_path = test_kit.testpmd.dpdk_build_path.joinpath("examples")
        server_app_path = examples_path.joinpath(server_app_name)
        client_app_path = examples_path.joinpath(client_app_name)

        # EAL -l: start server on cores 1-2,
        # EAL -n: use 4 memory channels
        # APP: -p : set port bitmask to port 0 and 1
        # APP: -n : allow one client to connect
        server_proc = node.execute_async(
            (
                f"{server_app_path} -l 1-2 -n 4 "
                f"-b {node.nics.get_nic_by_index(0).pci_slot} -- -p 3 -n 1"
            ),
            sudo=True,
            shell=True,
        )

        # Wait for server to finish init
        server_proc.wait_output("APP: Finished Process Init.", timeout=5)

        # EAL -l: start client on core 3,
        # EAL --proc-type: client runs as secondary process.
        # APP: -n : client index is 0
        client_result = node.execute(
            (
                f"timeout -s INT 2 {client_app_path} --proc-type=secondary -l 3 -n 4"
                f" -b {node.nics.get_nic_by_index(0).pci_slot} -- -n 0"
            ),
            sudo=True,
            shell=True,
        )

        # client blocks and returns, kill server once client is finished.
        kill.by_name(str(server_app_name), signum=SIGINT)
        server_result = server_proc.wait_result()

        # perform the checks from v2
        assert_that(client_result.stdout).described_as(
            "Secondary process did not finish initialization"
        ).contains("APP: Finished Process Init")

        assert_that(client_result.stdout).described_as(
            "Secondary process did not start accepting packets from server"
        ).contains("Client process 0 handling packets")

        # mp_client returns a nonstandard positive number when killed w signal.
        # one would expect either 0 or 130 (killed by signal w sigint).
        # check that the nonsense number is at least the expected one.
        assert_that(client_result.exit_code).described_as(
            "dpdk-mp client exit code was unexpected"
        ).is_equal_to(124)
        assert_that(server_result.exit_code).is_equal_to(0)

    @TestCaseMetadata(
        description="""
            test sriov failsafe during vf revoke (receive side)
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_sriov_rescind_failover_receiver(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        test_kits = init_nodes_concurrent(environment, log, variables, "failsafe")

        try:
            check_send_receive_compatibility(test_kits)
        except UnsupportedPackageVersionException as err:
            raise SkippedException(err)

        sender, receiver = test_kits

        # Want to only switch receiver sriov to avoid timing weirdness
        receiver.switch_sriov = True
        sender.switch_sriov = False

        kit_cmd_pairs = generate_send_receive_run_info("failsafe", sender, receiver)

        run_testpmd_concurrent(
            kit_cmd_pairs, DPDK_VF_REMOVAL_MAX_TEST_TIME, log, rescind_sriov=True
        )

        rescind_tx_pps_set = receiver.testpmd.get_mean_rx_pps_sriov_rescind()
        self._check_rx_or_tx_pps_sriov_rescind("RX", rescind_tx_pps_set)

    @TestCaseMetadata(
        description="""
            test sriov failsafe during vf revoke (send only version)
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_sriov_rescind_failover_send_only(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        test_kit = initialize_node_resources(node, log, variables, "failsafe")
        testpmd = test_kit.testpmd
        test_nic = node.nics.get_nic_by_index()
        testpmd_cmd = testpmd.generate_testpmd_command(
            test_nic, 0, "txonly", "failsafe"
        )
        kit_cmd_pairs = {
            test_kit: testpmd_cmd,
        }

        run_testpmd_concurrent(
            kit_cmd_pairs, DPDK_VF_REMOVAL_MAX_TEST_TIME, log, rescind_sriov=True
        )

        rescind_tx_pps_set = testpmd.get_mean_tx_pps_sriov_rescind()
        self._check_rx_or_tx_pps_sriov_rescind("TX", rescind_tx_pps_set)

    def _check_rx_or_tx_pps_sriov_rescind(
        self, tx_or_rx: str, pps: Tuple[int, int, int]
    ) -> None:
        before_rescind, during_rescind, after_reenable = pps
        self._check_rx_or_tx_pps(tx_or_rx, before_rescind, sriov_enabled=True)
        self._check_rx_or_tx_pps(tx_or_rx, during_rescind, sriov_enabled=False)
        self._check_rx_or_tx_pps(tx_or_rx, after_reenable, sriov_enabled=True)

    def _check_rx_or_tx_pps(
        self, tx_or_rx: str, pps: int, sriov_enabled: bool = True
    ) -> None:
        if sriov_enabled:
            assert_that(pps).described_as(
                f"{tx_or_rx}-PPS ({pps}) should have been greater "
                "than 2^20 (~1m) PPS before sriov disable."
            ).is_greater_than(2**20)
        else:
            assert_that(pps).described_as(
                f"{tx_or_rx}-PPS ({pps}) should have been less "
                "than 2^20 (~1m) PPS after sriov disable."
            ).is_less_than(2**20)

    @TestCaseMetadata(
        description="""
            verify vpp is able to detect azure network interfaces
            1. run fd.io vpp install scripts
            2. install vpp from their repositories
            3. start vpp service
            4. check that azure interfaces are detected by vpp
        """,
        priority=4,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
        ),
    )
    def verify_dpdk_vpp(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:

        initialize_node_resources(node, log, variables, "failsafe")

        vpp = node.tools[DpdkVpp]
        vpp.install()

        net = node.nics
        nic = net.get_nic_by_index()

        # set devices to down and restart vpp service
        ip = node.tools[Ip]
        for dev in [nic.lower, nic.upper]:
            ip.down(dev)
        for dev in [nic.lower, nic.upper]:
            ip.addr_flush(dev)

        vpp.start()
        vpp.run_test()

    @TestCaseMetadata(
        description="""
            This test runs the dpdk ring ping utility from:
            https://github.com/shemminger/dpdk-ring-ping
            to measure the maximum latency for 99.999 percent of packets during
            the test run. The maximum should be under 200000 nanoseconds
            (.2 milliseconds).
            Not dependent on any specific PMD.
        """,
        priority=4,
        requirement=simple_requirement(
            min_core_count=8,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_ring_ping(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        # ring ping requires dpdk source to run, since default is package_manager
        # we special case here to use to dpdk-stable as the default.
        self._force_dpdk_default_source(variables)
        # setup and unwrap the resources for this test
        test_kit = initialize_node_resources(node, log, variables, "failsafe")
        testpmd = test_kit.testpmd

        # grab a nic and run testpmd
        git = node.tools[Git]
        make = node.tools[Make]
        echo = node.tools[Echo]
        rping_build_env_vars = [
            "export RTE_TARGET=build",
            f"export RTE_SDK={str(testpmd.dpdk_path)}",
        ]
        echo.write_to_file(
            ";".join(rping_build_env_vars), node.get_pure_path("~/.bashrc"), append=True
        )
        git_path = git.clone(
            "https://github.com/shemminger/dpdk-ring-ping.git", cwd=node.working_path
        )
        make.run(
            shell=True,
            cwd=git_path,
            expected_exit_code=0,
            expected_exit_code_failure_message="make could not build rping project.",
        ).assert_exit_code()
        # run ringping for 30 seconds
        runcmd = "./build/rping -c 0x03 -n 2 --no-pci --no-huge -- -d 5 -t 10"
        result = node.execute(
            runcmd,
            shell=True,
            cwd=git_path,
            expected_exit_code=0,
            expected_exit_code_failure_message="rping program failed to run correctly.",
        )
        result.assert_exit_code()
        # get the max latency for 99.999 percent of enqueued 'packets'.
        result_regex = self._ring_ping_percentile_regex.search(result.stdout)
        if result_regex and len(result_regex.groups()) == 1:
            max_ping_measured = int(result_regex.group(1))
            assert_that(max_ping_measured).described_as(
                (
                    f"RingPing measured {max_ping_measured} as maximum ping latency,"
                    f" maximum should be less than {MAX_RING_PING_LIMIT_NS}"
                )
            ).is_less_than(MAX_RING_PING_LIMIT_NS)
        else:
            fail(
                (
                    "Could not get latency data from rping result. "
                    f"Search was for 'percentile 99.999 = ([0-9]+)'\n{result.stdout}\n"
                )
            )

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for default failsafe driver setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_send_receive_multi_txrx_queue_failsafe(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        try:
            verify_dpdk_send_receive_multi_txrx_queue(
                environment, log, variables, "failsafe"
            )
        except UnsupportedPackageVersionException as err:
            raise SkippedException(err)

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for default failsafe driver setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_send_receive_multi_txrx_queue_netvsc(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        try:
            verify_dpdk_send_receive_multi_txrx_queue(
                environment, log, variables, "netvsc"
            )
        except UnsupportedPackageVersionException as err:
            raise SkippedException(err)

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for default failsafe driver setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_send_receive_failsafe(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        try:
            verify_dpdk_send_receive(environment, log, variables, "failsafe")
        except UnsupportedPackageVersionException as err:
            raise SkippedException(err)

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for direct netvsc pmd setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_core_count=8,
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_dpdk_send_receive_netvsc(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        try:
            verify_dpdk_send_receive(environment, log, variables, "netvsc")
        except UnsupportedPackageVersionException as err:
            raise SkippedException(err)

    @TestCaseMetadata(
        description="""
          Run the L3 forwarding test for DPDK
        """,
        priority=4,
        requirement=simple_requirement(
            min_core_count=8,
            min_count=2,
            min_nic_count=3,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
        ),
    )
    def verify_dpdk_l3_forward(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        # multiprocess test requires dpdk source.
        forwarder, sender = environment.nodes.list()
        self._force_dpdk_default_source(variables)
        pmd = "failsafe"
        server_app_name = "dpdk-l3fwd"

        # initialize DPDK with sample applications selected for build
        test_kit = initialize_node_resources(
            forwarder, log, variables, pmd, sample_apps=["l3fwd"]
        )
        test_port = 0xD007

        # enable hugepages needed for dpdk EAL
        init_hugepages(forwarder)

        # get test basic info
        forwarder_ip = forwarder.nics.get_nic_by_index().ip_addr
        forwarder_device = forwarder.nics.get_nic_by_index().pci_slot
        sender_ip = sender.nics.get_nic_by_index().ip_addr

        # setup forwarding rules
        sample_rules = f"R{sender_ip}/32 {test_port}"
        rules_path = forwarder.get_pure_path("forwarding_rules")
        forwarder.tools[Echo].write_to_file(sample_rules, rules_path, append=True)

        # get binary path and start the forwarder
        examples_path = test_kit.testpmd.dpdk_build_path.joinpath("examples")
        server_app_path = examples_path.joinpath(server_app_name)
        fwd_cmd = (
            f"{server_app_path} -a {forwarder_device} --"
            f"-P --rule-ipv4={rules_path.as_posix()} "
        )
        forwarder.execute_async(
            fwd_cmd,
            sudo=True,
            shell=True,
        )

        # start the listener and start sending data to the forwarder
        listener = sender.execute_async(f"nc -l {sender_ip} {test_port}")
        sender.tools[Timeout].run_with_timeout(
            f"cat of=/dev/random | nc {forwarder_ip} {test_port}",
            timeout=60,
            kill_timeout=70,
        )

        # kill everything
        forwarder.tools[Kill].by_name(
            {server_app_name}, signum=SIGINT, ignore_not_exist=True
        )
        listener.kill()

    @TestCaseMetadata(
        description="""
            UIO basic functionality test.
            - Bind interface to uio_hv_generic
            - check that sysfs entry is created
            - unbind
            - check that the driver is unloaded.
            - rebind to original driver
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
            unsupported_features=[Gpu, Infiniband],
            # supported_features=[IsolatedResource],
        ),
    )
    def verify_uio_binding(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        lsmod = node.tools[Lsmod]
        modprobe = node.tools[Modprobe]
        nic = node.nics.get_nic_by_index()
        node.nics.get_nic_driver(nic.upper)
        if nic.bound_driver == "hv_netvsc":
            enable_uio_hv_generic_for_nic(node, nic)

        original_driver = nic.driver_sysfs_path
        node.nics.unbind(nic)
        node.nics.bind(nic, UIO_HV_GENERIC_SYSFS_PATH)

        node.execute(
            "test -e /dev/uio0",
            shell=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                "/dev/uio0 did not exist after driver bind"
            ),
        )
        assert_that(lsmod.module_exists("uio_hv_generic", force_run=True)).described_as(
            "uio_hv_generic was not found after bind"
        ).is_true()

        node.nics.unbind(nic)
        node.nics.bind(nic, str(original_driver))
        nic.bound_driver = node.nics.get_nic_driver(nic.upper)

        assert_that(nic.bound_driver).described_as(
            (
                "Driver after unbind/rebind was unexpected. "
                f"Expected hv_netvsc, found {nic.bound_driver}"
            )
        ).is_equal_to("hv_netvsc")

        modprobe.remove(["uio_hv_generic"])
        node.execute(
            "test -e /dev/uio0",
            shell=True,
            expected_exit_code=1,
            expected_exit_code_failure_message=(
                "/dev/uio0 still exists after driver unload"
            ),
        )

    def _force_dpdk_default_source(self, variables: Dict[str, Any]) -> None:
        if not variables.get("dpdk_source", None):
            variables["dpdk_source"] = DPDK_STABLE_GIT_REPO

    def after_case(self, log: Logger, **kwargs: Any) -> None:
        environment: Environment = kwargs.pop("environment")
        for node in environment.nodes.list():
            # reset SRIOV to enabled if left disabled
            interface = node.features[NetworkInterface]
            if not interface.is_enabled_sriov():
                log.debug("DPDK detected SRIOV was left disabled during cleanup.")
                interface.switch_sriov(enable=True, wait=False, reset_connections=True)

            # cleanup driver changes
            modprobe = node.tools[Modprobe]
            if modprobe.module_exists("uio_hv_generic"):
                node.tools[Service].stop_service("vpp")
                modprobe.remove(["uio_hv_generic"])
                node.close()
                modprobe.reload(["hv_netvsc"])
