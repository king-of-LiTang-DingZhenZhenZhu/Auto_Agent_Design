; PCell introspection helper for analogskills.
;
; Entry point:
;   analogskills_pcellIntrospectToFile(
;     logicalName libName cellName viewName paramSpec orient instName
;     calibLib calibCell outFile
;   )
;
; paramSpec is list(list("param" "type" value) ...), matching dbCreateParamInst.

procedure(analogskills_jsonEscape(value)
  let((text out ch)
    if(value == nil then
      text = ""
    else
      if(stringp(value) then
        text = value
      else
        text = sprintf(nil "%L" value)
      )
    )
    out = ""
    foreach(ch parseString(text "")
      cond(
        ((ch == "\\") out = strcat(out "\\\\"))
        ((ch == "\"") out = strcat(out "\\\""))
        ((ch == "\n") out = strcat(out "\\n"))
        ((ch == "\t") out = strcat(out "\\t"))
        (t out = strcat(out ch))
      )
    )
    out
  )
)

procedure(analogskills_jsonString(value)
  strcat("\"" analogskills_jsonEscape(value) "\"")
)

procedure(analogskills_pointJson(pt)
  if(pt then
    sprintf(nil "[%.12g, %.12g]" xCoord(pt) yCoord(pt))
  else
    "[0, 0]"
  )
)

procedure(analogskills_bboxJson(bbox)
  if(bbox then
    sprintf(nil "[%.12g, %.12g, %.12g, %.12g]" xCoord(lowerLeft(bbox)) yCoord(lowerLeft(bbox)) xCoord(upperRight(bbox)) yCoord(upperRight(bbox)))
  else
    "null"
  )
)

procedure(analogskills_centerJson(bbox)
  let((ll ur)
    if(bbox then
      ll = lowerLeft(bbox)
      ur = upperRight(bbox)
      sprintf(nil "[%.12g, %.12g]" (xCoord(ll)+xCoord(ur))/2.0 (yCoord(ll)+yCoord(ur))/2.0)
    else
      "[0, 0]"
    )
  )
)

procedure(analogskills_layerName(fig)
  let((lpp)
    lpp = fig~>lpp
    if(lpp then car(lpp) else "")
  )
)

procedure(analogskills_purposeName(fig)
  let((lpp)
    lpp = fig~>lpp
    if(lpp && cdr(lpp) then cadr(lpp) else "")
  )
)

procedure(analogskills_writeJsonField(port name value isLast)
  fprintf(port "    %s: %s%s\n" analogskills_jsonString(name) value if(isLast then "" else ","))
)

procedure(analogskills_paramValueJson(paramType paramValue)
  cond(
    ((paramType == "boolean") if(paramValue then "true" else "false"))
    ((paramType == "int") sprintf(nil "%d" paramValue))
    ((paramType == "float") sprintf(nil "%.12g" paramValue))
    (t analogskills_jsonString(paramValue))
  )
)

procedure(analogskills_writeParamsJson(port paramSpec)
  let((item idx)
    fprintf(port "    \"params\": {")
    idx = 0
    foreach(item paramSpec
      when(idx > 0 fprintf(port ", "))
      fprintf(port "%s: %s"
        analogskills_jsonString(car(item))
        analogskills_paramValueJson(cadr(item) caddr(item))
      )
      idx = idx + 1
    )
    fprintf(port "},\n")
  )
)

procedure(analogskills_writeRequestJson(port logicalName libName cellName viewName paramSpec orient instName calibLib calibCell)
  fprintf(port "  \"request\": {\n")
  analogskills_writeJsonField(port "logical_name" analogskills_jsonString(logicalName) nil)
  analogskills_writeJsonField(port "lib_name" analogskills_jsonString(libName) nil)
  analogskills_writeJsonField(port "cell_name" analogskills_jsonString(cellName) nil)
  analogskills_writeJsonField(port "view_name" analogskills_jsonString(viewName) nil)
  analogskills_writeParamsJson(port paramSpec)
  analogskills_writeJsonField(port "orient" analogskills_jsonString(orient) nil)
  analogskills_writeJsonField(port "instance_name" analogskills_jsonString(instName) nil)
  analogskills_writeJsonField(port "calibration_lib" analogskills_jsonString(calibLib) nil)
  analogskills_writeJsonField(port "calibration_cell" analogskills_jsonString(calibCell) t)
  fprintf(port "  },\n")
)

procedure(analogskills_writePinsJson(port term)
  let((pins pin pinFig idx pinIdx)
    pins = term~>pins
    fprintf(port "      \"pins\": [")
    idx = 0
    foreach(pin pins
      foreach(pinFig pin~>figs
        when(idx > 0 fprintf(port ", "))
        fprintf(port "{\"terminal\": %s, \"pin_name\": %s, \"layer\": %s, \"purpose\": %s, \"bbox_um\": %s, \"center_um\": %s, \"source\": \"oa_pin\"}"
          analogskills_jsonString(term~>name)
          analogskills_jsonString(if(pin~>name then pin~>name else term~>name))
          analogskills_jsonString(analogskills_layerName(pinFig))
          analogskills_jsonString(analogskills_purposeName(pinFig))
          analogskills_bboxJson(pinFig~>bBox)
          analogskills_centerJson(pinFig~>bBox)
        )
        idx = idx + 1
      )
    )
    fprintf(port "]")
  )
)

procedure(analogskills_writeTermsJson(port master)
  let((terms term idx)
    terms = master~>terminals
    fprintf(port "  \"terms\": [\n")
    idx = 0
    foreach(term terms
      when(idx > 0 fprintf(port ",\n"))
      fprintf(port "    {\"name\": %s, " analogskills_jsonString(term~>name))
      analogskills_writePinsJson(port term)
      fprintf(port "}")
      idx = idx + 1
    )
    fprintf(port "\n  ],\n")
  )
)

procedure(analogskills_writeLabelsAndShapesJson(port master)
  let((fig idx labelIdx shapeIdx layer purpose text)
    fprintf(port "  \"labels\": [\n")
    labelIdx = 0
    foreach(fig master~>shapes
      when(member(fig~>objType list("label" "textDisplay"))
        text = if(fig~>theLabel then fig~>theLabel else if(fig~>label then fig~>label else ""))
        when(labelIdx > 0 fprintf(port ",\n"))
        fprintf(port "    {\"text\": %s, \"layer\": %s, \"purpose\": %s, \"xy_um\": %s}"
          analogskills_jsonString(text)
          analogskills_jsonString(analogskills_layerName(fig))
          analogskills_jsonString(analogskills_purposeName(fig))
          analogskills_pointJson(fig~>xy)
        )
        labelIdx = labelIdx + 1
      )
    )
    fprintf(port "\n  ],\n")

    fprintf(port "  \"conductive_shapes\": [\n")
    shapeIdx = 0
    foreach(fig master~>shapes
      layer = analogskills_layerName(fig)
      purpose = analogskills_purposeName(fig)
      when(member(layer list("M0" "M1" "M2" "M3" "M4" "M5" "M6" "M7" "M8" "M9" "M10" "PO" "OD" "CO" "MD" "VD" "NW" "PDK" "PODE_GATE" "VIA1" "VIA2" "VIA3" "VIA4" "VIA5" "VIA6" "VIA7" "VIA8" "VIA9"))
        when(fig~>bBox
          when(shapeIdx > 0 fprintf(port ",\n"))
          fprintf(port "    {\"layer\": %s, \"purpose\": %s, \"bbox_um\": %s, \"source\": \"shape\"}"
            analogskills_jsonString(layer)
            analogskills_jsonString(purpose)
            analogskills_bboxJson(fig~>bBox)
          )
          shapeIdx = shapeIdx + 1
        )
      )
    )
    fprintf(port "\n  ],\n")
  )
)

procedure(analogskills_writeResolvedParamsJson(port inst)
  ; Read the CDF after callbacks have evaluated the native PCell.  The
  ; requested parameter list is not sufficient for debugging a sweep because
  ; CDF callbacks may normalize or overwrite dependent values.
  let((cdf param idx)
    fprintf(port "{")
    idx = 0
    when(inst
      cdf = cdfGetInstCDF(inst)
      when(cdf
        foreach(param cdf~>parameters
          when(idx > 0 fprintf(port ", "))
          fprintf(port "%s: %s"
            analogskills_jsonString(param~>name)
            analogskills_jsonString(param~>value)
          )
          idx = idx + 1
        )
      )
    )
    fprintf(port "}")
  )
)

procedure(analogskills_pcellEvalFailed(master)
  let((failed)
    failed = nil
    when(master
      foreach(fig master~>shapes
        when(fig~>objType == "label" && fig~>theLabel == "pcellEvalFailed"
          failed = t
        )
      )
    )
    failed
  )
)

procedure(analogskills_pcellIntrospectToFile(logicalName libName cellName viewName paramSpec orient instName calibLib calibCell outFile)
  let((cv master inst ivpMaster port ok err effectiveView fallbackReason)
    ok = t
    err = nil
    effectiveView = viewName
    fallbackReason = ""
    cv = nil
    inst = nil
    master = nil
    unless(ddGetObj(calibLib)
      ddCreateLib(calibLib strcat("./" calibLib))
    )
    cv = dbOpenCellViewByType(calibLib calibCell "layout" "maskLayout" "w")
    when(cv
      foreach(fig cv~>shapes dbDeleteObject(fig))
      foreach(oldInst cv~>instances dbDeleteObject(oldInst))
      master = dbOpenCellViewByType(libName cellName viewName nil "r")
      if(master then
        inst = dbCreateParamInst(cv master instName 0:0 orient 1 paramSpec)
      else
        err = "failed to open PCell master"
      )
      when(inst && analogskills_pcellEvalFailed(inst~>master)
        dbDeleteObject(inst)
        inst = nil
        fallbackReason = "layout PCell evaluation failed with pcellEvalFailed marker"
      )
      unless(inst
        ivpMaster = dbOpenCellViewByType(libName cellName "ivpcell" nil "r")
        if(ivpMaster then
          inst = dbCreateInst(cv ivpMaster instName 0:0 orient 1)
          effectiveView = "ivpcell"
          when(fallbackReason == "" fallbackReason = "layout PCell instance unavailable")
        else
          when(fallbackReason == "" fallbackReason = "ivpcell fallback master unavailable")
        )
      )
    )
    unless(inst
      ok = nil
      unless(err err = "failed to create PCell instance")
    )

    port = outfile(outFile)
    fprintf(port "{\n")
    analogskills_writeRequestJson(port logicalName libName cellName viewName paramSpec orient instName calibLib calibCell)
    fprintf(port "  \"master_bbox_um\": %s,\n" if(inst then analogskills_bboxJson(inst~>master~>bBox) else "null"))
    fprintf(port "  \"instance_bbox_um\": %s,\n" if(inst then analogskills_bboxJson(inst~>bBox) else "null"))
    if(inst then
      progn(
        analogskills_writeTermsJson(port inst~>master)
        analogskills_writeLabelsAndShapesJson(port inst~>master)
        fprintf(port "  \"warnings\": [],\n")
        fprintf(port "  \"errors\": [],\n")
      )
    else
      progn(
        fprintf(port "  \"terms\": [],\n")
        fprintf(port "  \"labels\": [],\n")
        fprintf(port "  \"conductive_shapes\": [],\n")
        fprintf(port "  \"warnings\": [],\n")
        fprintf(port "  \"errors\": [%s],\n" analogskills_jsonString(err))
      )
    )
    fprintf(port "  \"metadata\": {\"source\": \"analogskills_pcell_introspect.il\", \"effective_view\": %s, \"fallback_reason\": %s, \"resolved_params\": "
      analogskills_jsonString(effectiveView)
      analogskills_jsonString(fallbackReason)
    )
    analogskills_writeResolvedParamsJson(port inst)
    fprintf(port "}\n")
    fprintf(port "}\n")
    close(port)
    when(cv dbSave(cv) dbClose(cv))
    outFile
  )
)
