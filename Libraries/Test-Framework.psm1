function Get-SecretParams {
    <#
    .DESCRIPTION
    Used only if the "SECRET_PARAMS" parameter exists in the test definition xml.
    Used to specify parameters that should be passed to test script but cannot be
    present in the xml test definition or are unknown before runtime.
    #>

    param(
        [array]$ParamsArray,
        [xml]$XMLConfig
    )

    $platform = $XMLConfig.config.CurrentTestPlatform
    $testParams = @{}

    foreach ($param in $ParamsArray) {
        switch ($param) {
            "Password" {
                $value = $($XMLConfig.config.$platform.Deployment.Data.Password)
                $testParams["PASSWORD"] = $value
            }
            "RoleName" {
                $value = $AllVMData.RoleName
                $testParams["ROLENAME"] = $value
            }
            "Distro" {
                $value = $detectedDistro
                $testParams["DETECTED_DISTRO"] = $value
            }
            "Ipv4" {
                $value = $AllVMData.PublicIP
                $testParams["ipv4"] = $value
            }
        }
    }

    return $testParams
}

function Parse-TestParameters {
    <#
    .DESCRIPTION
    Converts the parameters specified in the test definition into a hashtable
    to be used later in test.
    #>

    param(
        $XMLParams,
        $XMLConfig
    )

    $testParams = @{}
    foreach ($param in $XMLParams.param) {
        $name = $param.split("=")[0]
        if ($name -eq "SECRET_PARAMS") {
            $paramsArray = $param.split("=")[1].trim("(",")"," ").split(" ")
            $testParams += Get-SecretParams -ParamsArray $paramsArray `
                 -XMLConfig $XMLConfig
        } else {
            $value = $param.split("=")[1]
            $testParams[$name] = $value
        }
    }

    return $testParams
}

function Run-SetupScript {
    <#
    .DESCRIPTION
    Executes a powershell script specified in the <setupscript> tag
    Used to further prepare environment/VM
    #>

    param(
        [string]$Script,
        [hashtable]$Parameters
    )
    $workDir = Get-Location
    $scriptLocation = Join-Path $workDir $Script
    $scriptParameters = ""
    foreach ($param in $Parameters.Keys) {
        $scriptParameters += "${param}=$($Parameters[$param]);"
    }
    $msg = ("Test setup/cleanup started using script:{0} with parameters:{1}" `
             -f @($Script,$scriptParameters))
    LogMsg $msg
    $result = & "${scriptLocation}" -TestParams $scriptParameters
    return $result
}

function Create-ConstantsFile {
    <#
    .DESCRIPTION
    Generic function that creates the constants.sh file using a hashtable
    #>

    param(
        [string]$FilePath,
        [hashtable]$Parameters
    )

    Set-Content -Value "#Generated by LISAv2" -Path $FilePath -Force
    foreach ($param in $Parameters.Keys) {
        Add-Content -Value ("{0}={1}" `
                 -f @($param,$($Parameters[$param]))) -Path $FilePath -Force
        $msg = ("{0}={1} added to constants.sh file" `
                 -f @($param,$($Parameters[$param])))
        LogMsg $msg
    }
}

function Run-TestScript {
    <#
    .DESCRIPTION
    Executes test scripts specified in the <testScript> tag.
    Supports python, shell and powershell scripts.
    Python and shell scripts will be executed remotely.
    Powershell scripts will be executed host side.
    After the test completion, the method will collect logs 
    (for shell and python) and return the relevant test result.
    #>

    param(
        [string]$Script,
        [hashtable]$Parameters,
        [string]$LogDir,
        $VMData,
        $XMLConfig,
        [string]$Username,
        [string]$Password,
        [string]$TestName,
        [string]$TestLocation,
        [int]$Timeout
    )

    $workDir = Get-Location
    $scriptName = $Script.split(".")[0]
    $scriptExtension = $Script.split(".")[1]
    $constantsPath = Join-Path $workDir "constants.sh"
    $result = $false
    $testResult = ""

    Create-ConstantsFile -FilePath $constantsPath -Parameters $Parameters
    foreach ($VM in $VMData) {
        RemoteCopy -upload -uploadTo $VM.PublicIP -Port $VM.SSHPort `
             -files $constantsPath -Username $Username -password $Password
        LogMsg "Constants file uploaded to: $($VM.RoleName)"
    }
    LogMsg "Test script: ${Script} started."
    if ($scriptExtension -eq "sh") {
        RunLinuxCmd -Command "echo '${Password}' | sudo -S -s eval `"export HOME=``pwd``;bash ${Script} > ${TestName}_summary.log 2>&1`"" `
             -Username $Username -password $Password -ip $VMData.PublicIP -Port $VMData.SSHPort `
             -runMaxAllowedTime $Timeout
    } elseif ($scriptExtension -eq "ps1") {
        $scriptDir = Join-Path $workDir "Testscripts\Windows"
        $scriptLoc = Join-Path $scriptDir $Script
        foreach ($param in $Parameters.Keys) {
            $scriptParameters += (";{0}={1}" -f ($param,$($Parameters[$param])))
        }

        $testResult = & "${scriptLoc}" -TestParams $scriptParameters
    } elseif ($scriptExtension -eq "py") {
        RunLinuxCmd -Username $Username -password $Password -ip $VMData.PublicIP -Port $VMData.SSHPort `
             -Command "python ${Script}" -runMaxAllowedTime $Timeout -runAsSudo
        RunLinuxCmd -Username $Username -password $Password -ip $VMData.PublicIP -Port $VMData.SSHPort `
             -Command "mv Runtime.log ${TestName}_summary.log" -runAsSudo
    }

    if (-not $testResult) {
        $testResult = Collect-TestLogs -LogsDestination $LogDir -ScriptName $scriptName -TestType $scriptExtension `
             -PublicIP $VMData.PublicIP -SSHPort $VMData.SSHPort -Username $Username -password $Password `
             -TestName $TestName
    }
    return $testResult
}

function Collect-TestLogs {
    <#
    .DESCRIPTION
    Collects logs created by the test script.
    The function collects logs only if a shell/python test script is executed.
    #>

    param(
        [string]$LogsDestination,
        [string]$ScriptName,
        [string]$PublicIP,
        [string]$SSHPort,
        [string]$Username,
        [string]$Password,
        [string]$TestType,
        [string]$TestName
    )
    # Note: This is a temporary solution until a standard is decided
    # for what string py/sh scripts return
    $resultTranslation = @{ "TestAborted" = "Aborted";
                            "TestFailed" = "FAIL";
                            "TestCompleted" = "PASS"
                          }

    if ($TestType -eq "sh") {
        $filesTocopy = "{0}/state.txt, {0}/summary.log, {0}/TestExecution.log, {0}/TestExecutionError.log" `
            -f @("/home/${Username}")
        RemoteCopy -download -downloadFrom $PublicIP -downloadTo $LogsDestination `
             -Port $SSHPort -Username "root" -password $Password `
             -files $filesTocopy
        $summary = Get-Content (Join-Path $LogDir "summary.log")
        $testState = Get-Content (Join-Path $LogDir "state.txt")
        $testResult = $resultTranslation[$testState]
    } elseif ($TestType -eq "py") {
        $filesTocopy = "{0}/state.txt, {0}/Summary.log, {0}/${TestName}_summary.log" `
            -f @("/home/${Username}")
        RemoteCopy -download -downloadFrom $PublicIP -downloadTo $LogsDestination `
             -Port $SSHPort -Username "root" -password $Password `
             -files $filesTocopy
        $summary = Get-Content (Join-Path $LogDir "Summary.log")
        $testResult = $summary
    }

    LogMsg "TEST SCRIPT SUMMARY ~~~~~~~~~~~~~~~"
    $summary | ForEach-Object {
        Write-Host $_ -ForegroundColor Gray -BackgroundColor White
    }
    LogMsg "END OF TEST SCRIPT SUMMARY ~~~~~~~~~~~~~~~"

    return $testResult
}

function Enable-RootUser {
    <#
    .DESCRIPTION
    Sets a new password for the root user for all VMs in deployment.
    #>

    param(
        $VMData,
        [string]$RootPassword,
        [string]$Username,
        [string]$Password
    )

    $deploymentResult = $True

    foreach ($VM in $VMData) {
        RemoteCopy -upload -uploadTo $VM.PublicIP -Port $VM.SSHPort `
             -files ".\Testscripts\Linux\enableRoot.sh" -Username $Username -password $Password
        $cmdResult = RunLinuxCmd -Command "bash enableRoot.sh -password ${RootPassword}" -runAsSudo `
             -Username $Username -password $Password -ip $VM.PublicIP -Port $VM.SSHPort
        if (-not $cmdResult) {
            LogMsg "Fail to enable root user for VM: $($VM.RoleName)"
        }
        $deploymentResult = $deploymentResult -and $cmdResult
    }

    return $deploymentResult
}

function Create-HyperVCheckpoint {
    <#
    .DESCRIPTION
    Creates new checkpoint for each VM in deployment.
    Supports Hyper-V only.
    #>

    param(
        $VMnames,
        $TestLocation,
        [string]$CheckpointName
    )

    foreach ($VMname in $VMnames) {
        Stop-VM -Name $VMname -TurnOff -Force -ComputerName `
            $TestLocation
        Set-VM -Name $VMname -CheckpointType Standard -ComputerName `
            $TestLocation
        Checkpoint-VM -Name $VMname -SnapshotName $CheckpointName -ComputerName `
            $TestLocation
        $msg = ("Checkpoint:{0} created for VM:{1}" `
                 -f @($CheckpointName,$VMName))
        LogMsg $msg
        Start-VM -Name $VMname -ComputerName $TestLocation
    }
}

function Apply-HyperVCheckpoint {
    <#
    .DESCRIPTION
    Applies existing checkpoint to each VM in deployment.
    Supports Hyper-V only.
    #>

    param(
        $VMnames,
        $TestLocation,
        [string]$CheckpointName
    )

    foreach ($VMname in $VMnames) {
        Stop-VM -Name $VMname -TurnOff -Force -ComputerName `
            $TestLocation
        Restore-VMSnapshot -Name $CheckpointName -VMName $VMname -Confirm:$false `
           -ComputerName $TestLocation
        $msg = ("VM:{0} restored to checkpoint: {1}" `
                 -f ($VMName,$CheckpointName))
        LogMsg $msg
        Start-VM -Name $VMname -ComputerName $TestLocation
    }
}

function Check-IP {
    <#
    .DESCRIPTION
    Checks if the ip exists (and SSH port is open) for each VM in deployment.
    Return a structure (similar to AllVMData) with updated information.
    Supports Hyper-V only.
    #>

    param(
        $VMData,
        $TestLocation,
        [string]$SSHPort,
        [int]$Timeout = 300
    )

    $newVMData = @()
    $runTime = 0

    while ($runTime -le $Timeout) {
        foreach ($VM in $VMData) {
            $publicIP = ""
            while (-not $publicIP) {
                LogMsg "$($VM.RoleName) : Waiting for IP address..."
                $vmNic = Get-VM -Name $VM.RoleName -ComputerName `
                    $TestLocation | Get-VMNetworkAdapter
                $vmIP = $vmNic.IPAddresses[0]
                if ($vmIP) {
                    $vmIP = $([ipaddress]$vmIP.trim()).IPAddressToString
                    $sshConnected = Test-TCP -testIP $($vmIP) -testport $($VM.SSHPort)
                    if ($sshConnected -eq "True") {
                        $publicIP = $vmIP
                    }
                }
                if (-not $publicIP) {
                    Start-Sleep 5
                    $runTime += 5
                }
            }
            $VM.PublicIP = $publicIP
            $newVMData += $VM
        }
        break
    }

    if ($runTime -gt $Timeout) {
        LogMsg "Cannot find IP for one or more VMs"
        throw "Cannot find IP for one or more VMs"
    } else {
        return $newVMData
    }
}

function Run-Test {
<#
    .SYNOPSIS
    Common framework used for test execution. Supports Azure and Hyper-V platforms.

    .DESCRIPTION
    The Run-Test function implements the existing LISAv2 methods into a common
    framework used to run tests. 
    The function is comprised of the next steps:

    - Test resource deployment step: 
        Deploys VMs and other necessary resources (network interfaces, virtual networks).
        Enables root user for all VMs in deployment.
        For Hyper-V it creates one snapshot for each VM in deployment.
    - Setup script execution step (Hyper-V only):
        Executes a setup script to further prepare environment/VMs.
        The script is specified in the test definition using the <SetupScript> tag.
    - Test dependency upload step:
        Uploads all files specified inside the test definition <files> tag.
    - Test execution step:
        Creates and uploads the constants.sh used to pass parameters to remote scripts
        (constants.sh file contains parameters specified in the <testParams> tag).
        Executes the test script specified in the <testScript> tag
        (the shell/python scripts will be executed remotely).
        Downloads logs for created by shell/python test scripts.
    - Test resource cleanup step:
        Removes deployed resources depending on the test result and parameters.

    .PARAMETER CurrentTestData
        Test definition xml structure.

    .PARAMETER XmlConfig
        Xml structure that contains all the relevant information about test/deployment.

    .PARAMETER Distro
        Distro under test.

    .PARAMETER VMUser
        Username used in all VMs in deployment.

    .PARAMETER VMPassword
        Password used in all VMs in deployment.

    .PARAMETER DeployVMPerEachTest
        Bool variable that specifies if the framework should create a new deployment for
        each test (and clean the deployment after each test).

    .PARAMETER ExecuteSetup
        Switch variable that specifies if the framework should create a new deployment
        (Used if DeployVMPerEachTest is False).

    .PARAMETER ExecuteTeardown
        Switch variable that specifies if the framework should clean the deployment
        (Used if DeployVMPerEachTest is False).
    #>

    param(
        $CurrentTestData,
        $XmlConfig,
        [string]$Distro,
        [string]$LogDir,
        [string]$VMUser,
        [string]$VMPassword,
        [bool]$DeployVMPerEachTest,
        [switch]$ExecuteSetup,
        [switch]$ExecuteTeardown
    )

    $result = ""
    $currentTestResult = CreateTestResultObject
    $resultArr = @()
    $testParameters = @{}
    $testPlatform = $XmlConfig.config.CurrentTestPlatform
    $testResult = $false
    $timeout = 300

    if ($testPlatform -eq "Azure") {
        $testLocation = $($xmlConfig.config.$TestPlatform.General.Location).Replace('"',"").Replace(' ',"").ToLower()
    } elseif ($testPlatform -eq "HyperV") {
        $testLocation = $xmlConfig.config.HyperV.Host.ServerName
    }

    if ($DeployVMPerEachTest -or $ExecuteSetup) {
        # Note: This method will create $AllVMData global variable
        $isDeployed = DeployVMS -setupType $CurrentTestData.setupType `
             -Distro $Distro -XMLConfig $XmlConfig
        Enable-RootUser -RootPassword $VMPassword -VMData $AllVMData `
             -Username $VMUser -password $VMPassword

        if ($testPlatform.ToUpper() -eq "HYPERV") {
            Create-HyperVCheckpoint -VMnames $AllVMData.RoleName -TestLocation `
                $testLocation -CheckpointName "ICAbase"
            $AllVMData = Check-IP -VMData $AllVMData -TestLocation $testLocation
            Set-Variable -Name AllVMData -Value $AllVMData -Scope Global
            Set-Variable -Name isDeployed -Value $isDeployed -Scope Global
        }
    } else {
        if ($testPlatform.ToUpper() -eq "HYPERV") {
            if ($CurrentTestData.AdditionalHWConfig.HyperVApplyCheckpoint -eq "False") {
                RemoveAllFilesFromHomeDirectory -allDeployedVMs $AllVMData
                LogMsg "Removed all files from home directory."
            } else  {
                Apply-HyperVCheckpoint -VMnames $AllVMData.RoleName -TestLocation `
                    $testLocation -CheckpointName "ICAbase"
                $AllVMData = Check-IP -VMData $AllVMData -TestLocation $testLocation
                Set-Variable -Name AllVMData -Value $AllVMData -Scope Global
                LogMsg "Public IP found for all VMs in deployment after checkpoint restore"
            }
        }
    }

    if ($CurrentTestData.TestParameters) {
        $testParameters = Parse-TestParameters -XMLParams $CurrentTestData.TestParameters `
             -XMLConfig $xmlConfig
    }

    if ($testPlatform -eq "Hyperv" -and $CurrentTestData.SetupScript) {
        foreach ($vmName in $AllVMData.RoleName) {
            if (Get-VM -Name $vmName -ComputerName `
                $testLocation -EA SilentlyContinue) {
                Stop-VM -Name $vmName -TurnOff -Force -ComputerName `
                    $testLocation
            }
            foreach ($script in $($CurrentTestData.SetupScript).Split(",")) {
                $setupResult = Run-SetupScript -Script $script `
                    -Parameters $testParameters
            }
            if (Get-VM -Name $vmName -ComputerName $testLocation `
                -EA SilentlyContinue) {
                Start-VM -Name $vmName -ComputerName `
                    $testLocation
            }
        }
    }

    if ($CurrentTestData.files) {
        # This command uploads test dependencies in the home directory for the $vmUsername user
        foreach ($VMData in $AllVMData) {
            RemoteCopy -upload -uploadTo $VMData.PublicIP -Port $VMData.SSHPort `
                 -files $CurrentTestData.files -Username $VMUser -password $VMPassword
            LogMsg "Test files uploaded to VM $($VMData.RoleName)"
        }
    }

    if ($CurrentTestData.Timeout) {
        $timeout = $CurrentTestData.Timeout
    }

    if ($CurrentTestData.TestScript) {
        $testResult = Run-TestScript -Script $CurrentTestData.TestScript `
             -Parameters $testParameters -LogDir $LogDir -VMData $AllVMData `
             -Username $VMUser -password $VMPassword -XMLConfig $XmlConfig `
             -TestName $currentTestData.testName -TestLocation $testLocation `
             -Timeout $timeout
        $resultArr += $testResult
    }

    $currentTestResult.TestResult = GetFinalResultHeader -resultarr $resultArr
    if ($DeployVMPerEachTest -or $ExecuteTeardown) {
        LogMsg "VM CLEANUP ~~~~~~~~~~~~~~~~~~~~~~~"
        $optionalParams = @{}
        if ($testParameters["SkipVerifyKernelLogs"] -eq "True" -or (-not $DeployVMPerEachTest)) {
            $optionalParams["SkipVerifyKernelLogs"] = $True
        }
        DoTestCleanUp -CurrentTestResult $CurrentTestResult -TestName $currentTestData.testName `
             -ResourceGroups $isDeployed @optionalParams
    }

    if ($testPlatform -eq "Hyperv" -and $CurrentTestData.CleanupScript) {
        foreach ($vmName in $AllVMData.RoleName) {
            if (Get-VM -Name $vmName -ComputerName `
                $testLocation -EA SilentlyContinue) {
                Stop-VM -Name $vmName -TurnOff -Force -ComputerName `
                    $testLocation
            }
            foreach ($script in $($CurrentTestData.CleanupScript).Split(",")) {
                $setupResult = Run-SetupScript -Script $script `
                    -Parameters $testParameters
            }
            if (Get-VM -Name $vmName -ComputerName $testLocation `
                -EA SilentlyContinue) {
                Start-VM -Name $vmName -ComputerName `
                    $testLocation
            }
        }
    }
  
    return $currentTestResult
}