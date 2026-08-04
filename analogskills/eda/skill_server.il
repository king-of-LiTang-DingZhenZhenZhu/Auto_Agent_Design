;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;  analogskills/eda/skill_server.il                    ;;
;;  Minimal Virtuoso <-> Python SKILL server bridge ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;;  Load this file in the Virtuoso CIW:
;;      load("analogskills/eda/skill_server.il")
;;
;;  Optional environment variables:
;;      SKILLS_Z_SKILL_SERVER_PATH  absolute path to skill_server.py
;;      PYTHON_EXE                  Python executable, default python3
;;      SKILLS_Z_PORT_FILE          port file, default skill_server_port.txt
;;

VSZ_port_file = "skill_server_port.txt"
VSZ_server_started = 0
VSZ_ipc_id = nil

procedure( VSZ_stdoutHandler(ipcId data)
    let( (result result_str)
        if( VSZ_server_started > 0 then
            printf("*INFO* Evaluate expression from analogskills server: %s
" data)
            if( result = errsetstring(data 't) then
                sprintf(result_str "%A
" car(result))
            else
                sprintf(result_str "%s
" car(nthelem(5 errset.errset)))
            )
            ipcWriteProcess(ipcId sprintf(nil "%d
" strlen(result_str)))
            ipcWriteProcess(ipcId result_str)
            't
        else
            if( data == "BAG skill server has started.  Yay!
" then
                VSZ_server_started = 1
                printf("*INFO* analogskills Python SKILL server started.
")
            else
                printf("*INFO* Waiting for analogskills Python server. Message: %s
" data)
            )
        )
    )
)

procedure( VSZ_stderrHandler(ipcId data)
    warn("analogskills Python server error: %s
" data)
    't
)

procedure( VSZ_exitHandler(ipcId exitId)
    printf("*INFO* analogskills Python server process exited with status: %d
" exitId)
    VSZ_server_started = 0
    't
)

procedure( VSZ_start_server()
    let( (script_path cmd python_exe work_dir env_port)
        VSZ_server_started = 0
        python_exe = getShellEnvVar("PYTHON_EXE")
        when( python_exe == nil python_exe = "python3" )
        env_port = getShellEnvVar("SKILLS_Z_PORT_FILE")
        when( env_port != nil VSZ_port_file = env_port )
        script_path = getShellEnvVar("SKILLS_Z_SKILL_SERVER_PATH")
        work_dir = getWorkingDir()

        if( script_path != nil && strlen(script_path) > 0 then
            cmd = sprintf(nil "PYTHONPATH=%s %s %s 5000 9999 %s" work_dir python_exe script_path VSZ_port_file)
        else
            cmd = sprintf(nil "PYTHONPATH=%s %s -m analogskills.eda.skill_server 5000 9999 %s" work_dir python_exe VSZ_port_file)
        )
        printf("*INFO* Starting analogskills Python SKILL server: %s
" cmd)
        VSZ_ipc_id = ipcBeginProcess(cmd "" 'VSZ_stdoutHandler 'VSZ_stderrHandler 'VSZ_exitHandler "")
        VSZ_ipc_id
    )
)

procedure( VSZ_stop_server()
    when( VSZ_ipc_id != nil
        ipcKillProcess(VSZ_ipc_id)
        VSZ_ipc_id = nil
        VSZ_server_started = 0
        printf("*INFO* analogskills Python SKILL server stopped.
")
    )
)

VSZ_start_server()
