"""Fast Structured Grid Navier-Stokes solver.
"""
import fasts
import FastS
__version__ = FastS.__version__

FIRST_IT = 0
HOOK     = None
HOOKIBC  = None

import numpy
import os
try:
    import Converter.PyTree as C
    import Converter.Internal as Internal
    import Connector
    import Connector.PyTree as X
    import Fast.Internal as FastI
    import KCore
    import math
    import timeit
    #import KCore.Dist as Dist
except:
    raise ImportError("FastS: requires Converter, Connector, Fast modules.")

try:
    OMP_NUM_THREADS = os.environ['OMP_NUM_THREADS']
    OMP_NUM_THREADS = int(OMP_NUM_THREADS)
except: OMP_NUM_THREADS = 1

# Variable alignement pour vectorisation
#CACHELINE = Dist.getCacheLine()

# transfered variables
varsN    = ['Density']
varsP    = ['Density_P1']
#varsN_SA = ['Density', 'VelocityX', 'VelocityY', 'VelocityZ', 'Temperature', 'TurbulentSANuTilde']

#==============================================================================
# compute in place
# graph is a dummy argument to be compatible with mpi version
#==============================================================================
def _compute(t, metrics, nitrun, tc=None, graph=None):
    global FIRST_IT, HOOK, HOOKIBC

    bases  = Internal.getNodesFromType1(t     , 'CGNSBase_t')       # noeud
    own   = Internal.getNodeFromName1(bases[0], '.Solver#ownData')  # noeud
    dtloc = Internal.getNodeFromName1(own     , '.Solver#dtloc')    # noeud

    zones = []
    for f in bases:
        zones += Internal.getNodesFromType1(f, 'Zone_t') 

    node = Internal.getNodeFromName1(bases[0], '.Solver#define')
    node = Internal.getNodeFromName1(node, 'omp_mode')
    omp_mode = 0
    if  node is not None: omp_mode = Internal.getValue(node)

    dtloc = Internal.getValue(dtloc) # tab numpy
    nitmax = int(dtloc[0])                 
    orderRk = int(dtloc[len(dtloc)-1])
    
    bcType = HOOKIBC[0]; Gamma=HOOKIBC[1]; Cv=HOOKIBC[2]; Mus=HOOKIBC[3]; Cs=HOOKIBC[4]; Ts=HOOKIBC[5]

    #### a blinder...
    itypcp = Internal.getNodeFromName2( zones[0], 'Parameter_int' )[1][29]
    #### a blinder...

    for nstep in xrange(1, nitmax+1): # pas RK ou ssiterations
       	# determination taille des zones a integrer (implicit ou explicit local)
    	hook1 = HOOK + fasts.souszones_list(zones, metrics, HOOK, nitrun, nstep)
        nidom_loc = hook1[11]
        
        # hook1[10] = nombre equations max
        # hook1[11] = nidom_lu
        # hook1[12] = lskip_lu
        # hook1[13] = lssiter_verif
        skip      = 0
        if (hook1[13] == 0 and nstep == nitmax and itypcp ==1): skip = 1

        # calcul Navier_stokes + appli CL
    	if nidom_loc > 0 and skip == 0:

            # Navier-Stokes
            fasts._computePT(zones, metrics, nitrun, nstep, omp_mode, hook1)

            # Ghostcell
            vars = varsP
            if  nstep == 2 and itypcp == 2 : vars = varsN  # Choix du tableau pour application transfer et BC
            timelevel_target = int(dtloc[4])
            _fillGhostcells(zones, tc, metrics, timelevel_target, vars, nstep, hook1) 

    # data update for unsteady joins
    dtloc[3] +=1   #time_level_motion
    dtloc[4] +=1   #time_level_target

    # switch pointers
    FastI.switchPointers__(zones, orderRk)
    # flag pour derivee temporelle 1er pas de temps implicit
    HOOK[9]  = 1
    FIRST_IT = 1
    return None
#
#==============================================================================
# Calcul et retourne la metrique
#==============================================================================
def metric(t):
    #global FIRST_IT
    zones        = Internal.getZones(t)
    dtloc        = Internal.getNodeFromName3(t, '.Solver#dtloc')
    dtloc_numpy  = Internal.getValue(dtloc)
    nssiter      = int(dtloc_numpy[0])
    #FIRST_IT     = int(dtloc_numpy[2])

    metrics=[]; motion ='none'
    for z in zones:
        b = Internal.getNodeFromName2(z, 'motion')
        if b is not None: motion = Internal.getValue(b)
        num = Internal.getNodeFromName1(z, '.Solver#ownData')
        if num is None:
            raise ValueError("metric: numerics is missing for zone %s."%z[0])
        if motion == 'rigid':
            grids = Internal.getNodesFromType1(z, 'GridCoordinates_t')
            if len(grids) == 1:
               grid_init = Internal.copyTree(grids[0])
               grid_init[0] = 'GridCoordinates#Init'
               Internal.addChild(z, grid_init, pos=1) # first
        metrics.append(fasts.metric(z, nssiter))   
    return metrics

#==============================================================================
# Initialisation parametre calcul: calcul metric + var primitive + compactage 
# + alignement + placement DRAM
#==============================================================================
def warmup(t, tc, graph=None, infos_ale=None, Adjoint=False, tmy=None):
    global FIRST_IT, HOOK, HOOKIBC
    # Get omp_mode
    omp_mode = 0
    node = Internal.getNodeFromName2(t, '.Solver#define')
    if node is not None:
        node = Internal.getNodeFromName1(node, 'omp_mode')
        if  node is not None: omp_mode = Internal.getValue(node)

    # Reordone les zones pour garantir meme ordre entre t et tc
    FastI._reorder(t, tc, omp_mode)

    # Construction param_int et param_real des zones
    _buildOwnData(t)
    # Calul de la metric: tijk, ventijk, ssiter_loc
    metrics = metric(t)
    # Contruction BC_int et BC_real pour CL
    _BCcompact(t) 
    # compact + align + init numa
    rmConsVars=True
    adjoint=Adjoint
    #t0=timeit.default_timer()
    t = createPrimVars(t, omp_mode, rmConsVars, adjoint)
    #t1=timeit.default_timer()
    #print "cout createprimvars= ", t1-t0

    # determination taille des zones a integrer (implicit ou explicit local)
    #evite probleme si boucle en temps ne commence pas a it=0 ou it=1. ex: xrange(22,1000)
    dtloc = Internal.getNodeFromName3(t, '.Solver#dtloc')  # noeud
    dtloc = Internal.getValue(dtloc)                       # tab numpy
    zones = Internal.getZones(t)

    #Allocation HOOK
    #t0=timeit.default_timer()
    f_it = FIRST_IT
    if HOOK is None: HOOK = FastI.createWorkArrays__(zones, dtloc, f_it ); FIRST_IT = f_it
    for nstep in xrange(1, int(dtloc[0])+1): hook1 = HOOK + fasts.souszones_list(zones, metrics, HOOK, 1, nstep)
    #t1=timeit.default_timer()
    #print "cout create workarray= ", t1-t0

    #Allocation HOOKIBC
    if HOOKIBC is None: HOOKIBC = FastI.getIBCInfo__(t)

    #corection pointeur ventijk si ale=0: pointeur Ro perdu par compact.
    #t0=timeit.default_timer()
    c   = 0
    ale = False
    for z in zones:
        motion = 'none'
        b = Internal.getNodeFromName2(z, 'motion')
        if b is not None: motion = Internal.getValue(b)
        if motion == 'none':
            sol = Internal.getNodeFromName1(z, 'FlowSolution#Centers')
            ro = Internal.getNodeFromName1(sol, 'Density')
            metrics[c][2] = ro[1]
        else: ale = True
        c += 1
    #t1=timeit.default_timer()
    #print "cout mise a jour pointer= ", t1-t0

    #
    # mise a jour vitesse entrainememnt
    #
    #t0=timeit.default_timer()
    if(ale == True and infos_ale is not None):
        print "ale actif. Teta et tetap=", infos_ale
        teta = infos_ale[0];  tetap = infos_ale[1]
        _motionlaw(t, teta, tetap)
        _computeVelocityAle(t,metrics)
    #t1=timeit.default_timer()
    #print "cout mise a jour vitesse entr= ", t1-t0

    #
    # Compactage arbre transfert
    #
    if tc is not None:
       if graph is not None: 
          g = graph['procDict']
          l = graph['procList']
       else: 
          g = None; l = None

       #t0=timeit.default_timer()
       X.miseAPlatDonnorTree__(zones, tc, procDict=g, procList=l)
       #t1=timeit.default_timer()
       #print "cout tc compact= ", t1-t0

    #
    # Compactage arbre moyennes stat
    #
    if tmy is not None:
        sol = Internal.getNodesFromName3(tmy, 'FlowSolution#Centers')
        var = Internal.getNodesFromType1(sol[0] , 'DataArray_t')
        varmy=[]
        for v in var: varmy.append('centers:'+v[0])
        _compact(tmy, fields=varmy)

    #
    # remplissage ghostcells
    #
    hook1[12] = 0
    nstep     = 1
    nitrun    = 0
    #t0=timeit.default_timer()
    if infos_ale is not None and len(infos_ale) == 3: nitrun = infos_ale[2]
    timelevel_target = int(dtloc[4]) 
    _fillGhostcells(zones, tc, metrics, timelevel_target, ['Density'], nstep, hook1) 
    #t1=timeit.default_timer()
    #print "cout ghostcell= ", t1-t0
    
    #
    # initialisation Mut
    #
    hook1[12] = 0
    nstep     = 1
    nitrun    = 0
    if infos_ale is not None and len(infos_ale) == 3: nitrun = infos_ale[2]
    fasts._computePT_mut(zones, metrics, nitrun, nstep, omp_mode, hook1)

    if tmy is None: return (t, tc, metrics)
    else: return (t, tc, metrics, tmy)

#==============================================================================
def _compact(t, containers=[Internal.__FlowSolutionNodes__, Internal.__FlowSolutionCenters__], fields=None, mode=None):
    #global  CACHELINE
    if  mode is not None:
      if mode == -1: 
        thread_numa = -1
      else:
        thread_numa = mode%(OMP_NUM_THREADS)
        if thread_numa ==0: thread_numa = OMP_NUM_THREADS
    else:                    
      thread_numa = -1

    zones = Internal.getZones(t)
    for z in zones:
        ars = FastI.getFields2Compact__(z, containers, fields)
        sh = None ; size = None
        val = [] # valid fields
        for a in ars:
            a1 = a[1]
            if sh is None: sh = a1.shape; size = a1.size; val.append(a)
            elif a1.shape == sh: val.append(a)
        nfields = len(val)
        if nfields > 0:
            # Create an equivalent contiguous numpy [flat]
    	    #eq = KCore.empty(size*nfields, CACHELINE)
            eq = numpy.empty(nfields*size, dtype=numpy.float64)
            c = 0
            param_int = Internal.getNodeFromName2(z, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_compact: Parameter_int is missing for zone %s."%z[0])
            for a in val:
                #a[1] = a[1].reshape((size), order='Fortran')
                a1 = a[1]
                ## marc a1 = a[1]
                #print 'a0',a[0],a[1].shape
                # Copy elements
                ptr = a1.reshape((size), order='Fortran') # no copy I hope
                fasts.initNuma( ptr, eq, param_int, c, thread_numa )
                #fasts.initNuma( ptr, eq, param_int, c )
                ## marc ptr = a1.reshape((size), order='Fortran') # no copy I hope
                ## marc fasts.initNuma( ptr, eq, param_int, c )
                #fasts.initNuma( a[1], eq, param_int, c )
                #fasts.initNuma( ptr , eq, param_int, c )
                #eq[c*size:(c+1)*size] = ptr[:]   
                # Replace numpys with slice
                a[1] = eq[c*size:(c+1)*size]
                a[1] = a[1].reshape(sh, order='Fortran')
                ## marc a[1] = eq[c*size:(c+1)*size]
                ## marc a[1] = a[1].reshape(sh, order='Fortran')
                #print 'a1',a[0],a[1].shape
                ##a[1] = a[1].reshape( (size), order='Fortran')
                #print 'a',a[0],a[1].shape 

                c += 1
    return None


#==============================================================================
# For periodic unsteady chimera join, parameter must be updated peridicaly 
#==============================================================================
def _UpdateUnsteadyJoinParam(t, tc, omega, timelevelInfos, graph, tc_steady='tc_steady.cgns', directory='.'):

    bases = Internal.getNodesFromType1(t      , 'CGNSBase_t')       # noeud
    own   = Internal.getNodeFromName1(bases[0], '.Solver#ownData')  # noeud
    dtloc = None
    if own is not None: dtloc = Internal.getNodeFromName1(own     , '.Solver#dtloc')    # noeud

    #on cree les noeud infos insta pour chimere perio s'il n'existe pas 
    TimeLevelOpts=['TimeLevelMotion','TimeLevelTarget'] 
    for opt in TimeLevelOpts:
       tmp = Internal.getNodeFromName1(t, opt)
       Internal.createUniqueChild(t, opt, 'DataArray_t', value=0)

    if dtloc is not None:
      dtloc            = Internal.getValue(dtloc) # tab numpy
      timelevel_motion = dtloc[3]
      timelevel_target = dtloc[4]
    else:
      timelevel_motion = 0
      timelevel_target = 0

    timelevel_period = timelevelInfos["TimeLevelPeriod"]
    timelevel_360    = timelevelInfos["TimeLevel360"]
    timelevel_perfile= timelevelInfos["TimeLevelPerFile"]
    timelevel_axeRot = timelevelInfos["TimeLevelRotationAxis"]

    No_period = timelevel_motion//timelevel_period 

    #
    #target in no more in tc; need need data in a new file
    #
    if timelevel_target == timelevel_perfile+1 or tc == None: 

       tmp  = No_period*timelevel_period
       root = timelevel_perfile + ( (timelevel_motion - tmp)//timelevel_perfile)*timelevel_perfile

       FILE = tc_steady
       if os.access(FILE, os.F_OK): tc  = C.convertFile2PyTree(FILE)
       else: print "error reading", FILE
       FILE = directory+'/tc_'+str(root)+'.cgns'
       print 'File inst=', FILE, 'target=', timelevel_target, 'motionlevel=', timelevel_motion
       if os.access(FILE, os.F_OK): tc_inst  = C.convertFile2PyTree(FILE)
       else: print "error reading", FILE

       #UnsteadyConnectInfos  = X.getUnsteadyConnectInfos(tc_inst)
       tc = Internal.merge( [tc, tc_inst] )

       # Get omp_mode
       omp_mode = 0
       node = Internal.getNodeFromName2(t, '.Solver#define')
       if node is not None:
        node = Internal.getNodeFromName1(node, 'omp_mode')
        if  node is not None: omp_mode = Internal.getValue(node)

       # Reordone les zones pour garantir meme ordre entre t et tc
       FastI._reorder(t, tc, omp_mode)

       # Compactage arbre transfert
       g = None; l = None
       zones=Internal.getZones(t)
       X.miseAPlatDonnorTree__(zones, tc, procDict=g, procList=l)

       #Remise zero target
       if dtloc is not None: dtloc[4] = 0

    #
    #timelevel_motion larger than calculated peridicity; need to modify angle of rotation for azymuth periodicity
    #
    if timelevel_motion == timelevel_period+1: 
       bases  = Internal.getNodesFromType1(tc    , 'CGNSBase_t')       # noeud

       sign =-1
       if omega > 0: sign = 1
       for base in bases:
         if   base[0]=='Rotor': teta = -math.pi*timelevel_period/timelevel_360*No_period*sign
         elif base[0]=='Stator':teta =  math.pi*timelevel_period/timelevel_360*No_period*sign
         zones  = Internal.getNodesFromType1(base , 'Zone_t')       # noeud
         for z in zones:
           angles = Internal.getNodesFromName2(z, 'RotationAngle')
           for angle in angles: angle[1][:]= angle[1][:] + teta*timelevel_axeRot[:]

       
    #
    #timelevel_motion larger than number of timelevels for 360degre 
    #
    if timelevel_motion > timelevel_360: dtloc[3] = 0  # remise a zero du compteur si 360degres 

    return tc

#==============================================================================
# Converti les variables conservatives de l'arbre en variables primitives
# compactees
# IN: t: arbre devant contenir les variables conservatives aux centres
# IN: adim: etat de reference, on utilise uniquement cvInf pour la temperature.
# IN: rmConsVars: if True, remove the conservative variables
#==============================================================================
def createPrimVars(t, omp_mode, rmConsVars=True, Adjoint=False):
    tp = Internal.copyRef(t)
    _createPrimVars(tp, omp_mode, rmConsVars, Adjoint)
    return tp

#==============================================================================
def _createPrimVars(t, omp_mode, rmConsVars=True, Adjoint=False):
    global FIRST_IT
    bases = Internal.getNodesFromType1(t, 'CGNSBase_t')
    for b in bases:
        zones = Internal.getNodesFromType1(b, 'Zone_t')
        count = -1
        if omp_mode == 1: count = 0
        for z in zones:
            if omp_mode == 1: count += 1
#            sa,FIRST_IT  = FastI._createPrimVars(b, z, FIRST_IT, omp_mode)
            sa,FIRST_IT  = FastI._createPrimVars(b, z, FIRST_IT, omp_mode, rmConsVars,Adjoint)
            sfd = 0
            a = Internal.getNodeFromName2(z, 'sfd')
            if a is not None: sfd = Internal.getValue(a)
            if sa:
                t0=timeit.default_timer()
                _compact(z, fields=['centers:Density'   , 'centers:VelocityX'   , 'centers:VelocityY'   ,'centers:VelocityZ'   , 'centers:Temperature'   , 'centers:TurbulentSANuTilde']   , mode=count)
                t1=timeit.default_timer()
                #print "cout compact D= ", t1-t0, z[0]
                t0=timeit.default_timer()
                _compact(z, fields=['centers:Density_M1', 'centers:VelocityX_M1', 'centers:VelocityY_M1','centers:VelocityZ_M1', 'centers:Temperature_M1', 'centers:TurbulentSANuTilde_M1'], mode=count)
                t1=timeit.default_timer()
                #print "cout compact M= ", t1-t0, z[0]
                t0=timeit.default_timer()
                _compact(z, fields=['centers:Density_P1', 'centers:VelocityX_P1', 'centers:VelocityY_P1','centers:VelocityZ_P1', 'centers:Temperature_P1', 'centers:TurbulentSANuTilde_P1'], mode=count)
                t1=timeit.default_timer()
                #print "cout compact P= ", t1-t0, z[0]
                t0=timeit.default_timer()
                if sfd == 1:
                   _compact(z, fields=['centers:Density_f','centers:VelocityX_f','centers:VelocityY_f','centers:VelocityZ_f','centers:Temperature_f', 'centers:TurbulentSANuTilde_f'],mode=count)
            else:
                _compact(z, fields=['centers:Density'   , 'centers:VelocityX'   , 'centers:VelocityY'   ,'centers:VelocityZ'   , 'centers:Temperature'   ], mode=count)
                _compact(z, fields=['centers:Density_M1', 'centers:VelocityX_M1', 'centers:VelocityY_M1','centers:VelocityZ_M1', 'centers:Temperature_M1'], mode=count)
                _compact(z, fields=['centers:Density_P1', 'centers:VelocityX_P1', 'centers:VelocityY_P1','centers:VelocityZ_P1', 'centers:Temperature_P1'], mode=count)
                if sfd == 1:
                   _compact(z, fields=['centers:Density_f','centers:VelocityX_f','centers:VelocityY_f','centers:VelocityZ_f','centers:Temperature_f'],mode=count)

            # on compacte seulement pour recuperer le bon alignement
            if  (C.isNamePresent(z, 'centers:ViscosityEddy') == 1 and C.isNamePresent(z, 'centers:TurbulentDistance') == 1): _compact(z, fields=['centers:ViscosityEddy','centers:TurbulentDistance'], mode=count)
            elif(C.isNamePresent(z, 'centers:ViscosityEddy') != 1 and C.isNamePresent(z, 'centers:TurbulentDistance') == 1): _compact(z, fields=['centers:TurbulentDistance'], mode=count)
            elif(C.isNamePresent(z, 'centers:ViscosityEddy') == 1 and C.isNamePresent(z, 'centers:TurbulentDistance') != 1): _compact(z, fields=['centers:ViscosityEddy']    , mode=count)

            if  (C.isNamePresent(z, 'centers:cellN') == 1 ): _compact(z, fields=['centers:cellN'], mode=count)

            #  adjoint 
            if  (C.isNamePresent(z, 'centers:dpCLp_dpDensity') == 1): 
                _compact(z, fields=['centers:dpCDp_dpDensity','centers:dpCDp_dpMomentumX','centers:dpCDp_dpMomentumY','centers:dpCDp_dpMomentumZ','centers:dpCDp_dpEnergyStagDens'], mode=count)
                _compact(z, fields=['centers:dpCLp_dpDensity','centers:dpCLp_dpMomentumX','centers:dpCLp_dpMomentumY','centers:dpCLp_dpMomentumZ','centers:dpCLp_dpEnergyStagDens'], mode=count)
                _compact(z, fields=['centers:rhsIterAdjCLp_RDensity','centers:rhsIterAdjCLp_RMomentumX','centers:rhsIterAdjCLp_RMomentumY','centers:rhsIterAdjCLp_RMomentumZ','centers:rhsIterAdjCLp_REnergyStagDens'], mode=count)
                _compact(z, fields=['centers:rhsIterAdjCDp_RDensity','centers:rhsIterAdjCDp_RMomentumX','centers:rhsIterAdjCDp_RMomentumY','centers:rhsIterAdjCDp_RMomentumZ','centers:rhsIterAdjCDp_REnergyStagDens'], mode=count)

                _compact(z, fields=['centers:AdjCLp_RDensity','centers:AdjCLp_RMomentumX','centers:AdjCLp_RMomentumY','centers:AdjCLp_RMomentumZ','centers:AdjCLp_REnergyStagDens'], mode=count)
                _compact(z, fields=['centers:AdjCDp_RDensity','centers:AdjCDp_RMomentumX','centers:AdjCDp_RMomentumY','centers:AdjCDp_RMomentumZ','centers:AdjCDp_REnergyStagDens'], mode=count)
                _compact(z, fields=['dpCLp_dpX','dpCLp_dpY','dpCLp_dpZ','dpCDp_dpX','dpCDp_dpY','dpCDp_dpZ'], mode=count)

                _compact(z, fields=['centers:incAdj_RDensity','centers:incAdj_RMomentumX','centers:incAdj_RMomentumY','centers:incAdj_RMomentumZ','centers:incAdj_REnergyStagDens'], mode=count)

    return None
#==============================================================================
# Compact les solutions primitives ro,u,T (doivent exister dans t)
# Cree si necessaire les M1 et P1
#==============================================================================
def compactPrimVars(t):
    tp = Internal.copyRef(t)
    _compactPrimVars(tp)
    return tp

def _compactPrimVars(t):
    vars = C.getVarNames(t, excludeXYZ=True, loc='centers')
    if 'centers:Density_M1' not in vars:
        C._cpVars(t, 'centers:Density', t, 'centers:Density_M1')
        C._cpVars(t, 'centers:VelocityX', t, 'centers:VelocityX_M1')
        C._cpVars(t, 'centers:VelocityY', t, 'centers:VelocityY_M1')
        C._cpVars(t, 'centers:VelocityZ', t, 'centers:VelocityZ_M1')
        C._cpVars(t, 'centers:Temperature', t, 'centers:Temperature_M1')
    if 'centers:Density_P1' not in vars:
        C._cpVars(t, 'centers:Density', t, 'centers:Density_P1')
        C._cpVars(t, 'centers:VelocityX', t, 'centers:VelocityX_P1')
        C._cpVars(t, 'centers:VelocityY', t, 'centers:VelocityY_P1')
        C._cpVars(t, 'centers:VelocityZ', t, 'centers:VelocityZ_P1')
        C._cpVars(t, 'centers:Temperature', t, 'centers:Temperature_P1')
    _compact(t, fields=['centers:Density', 'centers:VelocityX', 'centers:VelocityY','centers:VelocityZ', 'centers:Temperature'])
    _compact(t, fields=['centers:Density_M1', 'centers:VelocityX_M1', 'centers:VelocityY_M1','centers:VelocityZ_M1', 'centers:Temperature_M1'])
    _compact(t, fields=['centers:Density_P1', 'centers:VelocityX_P1', 'centers:VelocityY_P1','centers:VelocityZ_P1', 'centers:Temperature_P1'])
    return None

#==============================================================================
def checkBalance(t):
    zones = Internal.getZones(t)

    size_reelle=0
    size_transf=0
    for z in zones:
       dim = Internal.getZoneDim(z)
       iv  = dim[1]-5
       jv  = dim[2]-5
       kv  = dim[3]-5
       trans =  4*(iv*jv + iv*kv + kv*jv)
       size_reelle = size_reelle +iv*jv*kv
       size_transf = size_transf + trans
       #print  iv, jv, kv, trans
    print "size Pb: totale =", size_reelle+size_transf, 'woghost=', size_reelle, 'NB ghost=',size_transf, 'Nbzone=',len(zones)
    return None

#==============================================================================
# Interface for Vtune/Advisor collection control
#==============================================================================
def itt(var):
    if var == 'pause':
          ivar =1
    else :
          ivar = 0
    print "itt collection (Vtune/Advisor)", var
    fasts.itt(ivar)
    return None
#==============================================================================
def _applyBC(t, metrics, var="Density"):
    zones = Internal.getZones(t)
    c = 0
    
    for z in zones: fasts._applyBC(z, metrics[c], var); c += 1
    return None

#==============================================================================
def _fillGhostcells(zones, tc, metrics, timelevel_target, vars, nstep, hook1): 

    # hook1[10] = nombre equations max
    # hook1[11] = nidom_lu
    # hook1[12] = lskip_lu
    # hook1[13] = lssiter_verif
   if hook1[12] ==0:

       #transfert
       if tc is not None :
           tc_compact = Internal.getNodeFromName1( tc, 'Parameter_real')
           #Si param_real n'existe pas, alors pas de raccord dans tc
           if tc_compact is not  None:

              param_real= tc_compact[1]
              param_int = Internal.getNodeFromName1(tc, 'Parameter_int' )[1]
              zonesD    = Internal.getZones(tc)

              if hook1[10] == 5: varType = 2
              else             : varType = 21

              bcType = HOOKIBC[0]; Gamma=HOOKIBC[1]; Cv=HOOKIBC[2]; Mus=HOOKIBC[3]; Cs=HOOKIBC[4]; Ts=HOOKIBC[5]

              if nstep <= 3: 
                 for v in vars: C._cpVars(zones, 'centers:'+v, zonesD, v)

              type_transfert = 2  # 0= ID uniquement, 1= IBC uniquement, 2= All
              no_transfert   = 1  # dans la list des transfert point a point
              Connector.connector.___setInterpTransfers(zones, zonesD, vars, param_int, param_real, timelevel_target, varType, bcType, type_transfert, no_transfert,Gamma,Cv,Mus,Cs,Ts)

       #apply BC
       _applyBC(zones, metrics, var=vars[0])

   return None
#==============================================================================
# Cree un noeud POST
# IN: t: tree
# IN: dir: direction de la moyenne '0', 'i', 'j', 'k', 'ij', 'ik', 'jk'
# IN: vars: variables concernees
# IN: nsample: nbre d'echantillons dans la moyenne
# OUT: return a tree with POST node
#==============================================================================
def createStatNodes(t, dir='0', vars=[], nsamples=0):
    try: import Transform.PyTree as T
    except: raise ImportError("createStatNodes: requires Transform module.")

    PostBaseName = 'POST' # nom de la base POST
    DataNodeName = '.Solver#post'
    vars0 = ['CoordinateX','CoordinateY','CoordinateZ']

    tmy = C.newPyTree([PostBaseName])

    b = Internal.getNodesFromName1(tmy, PostBaseName)

    varmy = ['MomentumX','MomentumY','MomentumZ','Density','Pressure','Pressure^2','ViscosityEddy','rou^2','rov^2','row^2','rouv','rouw','rovw']
    lgrad = 0
    for var in vars:
	if var == 'thermique':
		varmy += ['Temperature','T^2','rouT','rovT','rowT','Eps_T' ]
                lgrad =  1

    for i in xrange(len(varmy)):
	varmy[i] = 'centers:'+varmy[i]

    ##on determine le nbr de cellule fictive active pour le calcul des moyennes
    numcellfic = 2 
    ific       = 2   # a adapter en DF
    if lgrad == 1: numcellfic = 1

    zones = []
    for b0 in Internal.getNodesFromType1(t,'CGNSBase_t'):
        if b0[0] != PostBaseName:
            zones += Internal.getZones(b0)

    if dir == '0':
        for z in zones:
            #
            datap = numpy.empty((17), numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if dim[3] == 2: inck =  0
            zp = T.subzone(z, (1,1,1), (-1,-1,-1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)

            dim_my = Internal.getZoneDim(zp)
            datap[0]  = 0                                                # nbr direction homogene
            datap[1]  = 0                                                # dir homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1]-1)*(dim_my[2]-1)*(dim_my[3]-1)        # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = 1                                                # nbre cellule homogene
            datap[6]  = 1                                                # incerment i
            datap[7]  = dim_my[1]-1                                      # increment j
            datap[8]  =(dim_my[1]-1)*(dim_my[2]-1)                       # increment k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if dim_my[3] == 2: datap[10] =  0
            datap[11] =  1 - numcellfic                                  # loop moyenne imin
            datap[12] =  (dim_my[1]-1) -2*ific + numcellfic              # loop moyenne imax
            datap[13] =  1 - numcellfic                                  # loop moyenne jmin
            datap[14] =  dim_my[2]-1 -2*ific + numcellfic                   # loop moyenne jmax
            datap[15] =  1 - numcellfic*inck                             # loop moyenne kmin
            datap[16] =  dim_my[3]-1 -2*ific*inck + numcellfic*inck         # loop moyenne kmax
	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])

            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])
            b[0][2].append(zp)

    elif (dir == 'i'):
        for z in zones:
            #
            datap = numpy.empty((17), numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if (dim[3] == 2): inck =  0
            #
            zp = T.subzone(z, (1,1,1), (1,-1,-1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)
            #
            dim_tr = Internal.getZoneDim(zp)
            dim_my = [ 0, 1 , dim_tr[1], dim_tr[2] ]
            #print dim_my
            datap[0]  = 1                                                # nbr direction homogene
            datap[1]  = 1                                                # dir homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1])*(dim_my[2]-1)*(dim_my[3]-1)        # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = dim[1]-1 -2*ific                                 # nbre cellule homogene
            datap[6]  = 0                                                # incerment i
            datap[7]  = dim_my[1]-1                                      # increment j
            datap[8]  =(dim_my[1]-1)*(dim_my[2]-1)                       # increment k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if (dim[3] == 2): datap[10] =  0
            datap[11] =  1                                               # loop moyenne imin
            datap[12] =  1                                               # loop moyenne imax
            datap[13] =  1 - numcellfic                                  # loop moyenne jmin
            datap[14] =  datap[7] -2*ific + numcellfic                   # loop moyenne jmax
            datap[15] =  1 - numcellfic*inck                             # loop moyenne kmin
            datap[16] =  datap[8] -2*ific*inck + numcellfic*inck         # loop moyenne kmax

            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])

            ##modifie le moeud param_int de l'arbre tmy (issue de subzone) pour la fonction initNuma
            param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS

            param_int[1][0] = dim_my[1]             #nijk
            param_int[1][1] = dim_my[2]-1
            param_int[1][2] = dim_my[3]-1
            param_int[1][3] = datap[9]
            param_int[1][4] = datap[10]
            param_int[1][20]= 1                     #ijkv
            param_int[1][21]= dim_my[2]-1 -2*ific 
            param_int[1][22]= dim_my[3]-1 -2*ific*inck

	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])
            b[0][2].append(zp)

    elif (dir == 'j'):
        for z in zones:
            #
            datap = numpy.empty((17), numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if (dim[3] == 2): inck =  0
            #
            zp = T.subzone(z, (1,1,1), (-1,1,-1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)
            #
            dim_tr = Internal.getZoneDim(zp)
            dim_my = [ 0, dim_tr[1], 1, dim_tr[2] ]
            #print dim_my
            datap[0]  = 1                                                # nbr direction homogene
            datap[1]  = 2                                                # dir homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1]-1)*(dim_my[2])*(dim_my[3]-1)        # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = dim[2]-1  -2*ific                                # nbre cellule homogene
            datap[6]  = 1                                                # incerment i
            datap[7]  = 0                                                # increment j
            datap[8]  =(dim_my[1]-1)                                     # increment k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if (dim[3] == 2): datap[10] =  0
            datap[11] =  1 - numcellfic                                  # loop moyenne imin
            datap[12] =  datap[6] -2*ific + numcellfic                   # loop moyenne imax
            datap[13] =  1                                               # loop moyenne jmin
            datap[14] =  1                                               # loop moyenne jmax
            datap[15] =  1 - numcellfic*inck                             # loop moyenne kmin
            datap[16] =  datap[8] -2*ific*inck + numcellfic*inck         # loop moyenne kmax
            #print datap

            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])

            ##modifie le moeud param_int de l'arbre tmy (issue de subzone) pour la fonction initNuma
            param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS
            param_int[1][0] = dim_my[1]-1             #nijk
            param_int[1][1] = dim_my[2]
            param_int[1][2] = dim_my[3]-1
            param_int[1][3] = datap[9]
            param_int[1][4] = datap[10]
            param_int[1][20]= dim_my[2]-1 -2*ific   #ijkv
            param_int[1][21]= 1                    
            param_int[1][22]= dim_my[3]-1 -2*ific*inck
            #
	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])
            b[0][2].append(zp)

    elif (dir == 'k'):
        for z in zones:
            #
            datap = numpy.empty(17, numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if (dim[3] == 2): inck =  0
            #
            zp = T.subzone(z, (1,1,1), (-1,-1,1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)
            #
            dim_tr = Internal.getZoneDim(zp)
            dim_my = [ 0, dim_tr[1], dim_tr[2], 1 ]
            
            datap[0]  = 1                                                # nbr direction homogene
            datap[1]  = 3                                                # dir homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1]-1)*(dim_my[2]-1)*(dim_my[3])          # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = dim[3]-1  -2*ific*inck                           # nbre cellule homogene
            datap[6]  = 1                                                # nbre cellule i pour calcul adress
            datap[7]  = dim_my[1]-1                                      # nbre cellule j
            datap[8]  = 0                                                # nbre cellule k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if (dim[3] == 2): datap[10] =  0
            datap[11] =  1 - numcellfic                                  # loop moyenne imin
            datap[12] =  (dim_my[1]-1) -2*ific + numcellfic              # loop moyenne imax
            datap[13] =  1 - numcellfic                                  # loop moyenne jmin
            datap[14] =  (dim_my[2]-1) -2*ific + numcellfic              # loop moyenne jmax
            datap[15] =  1                                               # loop moyenne kmin
            datap[16] =  1                                               # loop moyenne kmax
            
            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])

            ##modifie le moeud param_int de l'arbre tmy (issue de subzone) pour la fonction initNuma
            param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS
            param_int[1][0] = dim_my[1]-1           #nijk
            param_int[1][1] = dim_my[2]-1
            param_int[1][2] = dim_my[3]
            param_int[1][3] = datap[9]
            param_int[1][4] = 0
            param_int[1][20]= dim_my[1]-1 -2*ific   #ijkv
            param_int[1][21]= dim_my[2]-1 -2*ific 
            param_int[1][22]= 1
            #
	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])
            b[0][2].append(zp)

    elif (dir == 'ij'):
        for z in zones:
            #
            datap = numpy.empty((17), numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if (dim[3] == 2): inck =  0
            #
            zp = T.subzone(z, (1,1,1), (1,1,-1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)
            #
            dim_tr = Internal.getZoneDim(zp)
            dim_my = [ 0, 1, 1, dim_tr[1] ]
            #print dim_my
            datap[0]  = 2                                                # nbr direction homogene
            datap[1]  = 3                                                # dir non homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1])*(dim_my[2])*(dim_my[3]-1)        # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = (dim[1]-1  -2*ific)*(dim[2]-1  -2*ific)          # nbre cellule homogene
            datap[6]  = 0                                                # nbre cellule i pour calcul adress
            datap[7]  = 0                                                # nbre cellule j
            datap[8]  = 1                                                # nbre cellule k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if (dim[3] == 2): datap[10] =  0
            datap[11] =  1                                               # loop moyenne imin
            datap[12] =  1                                               # loop moyenne imax
            datap[13] =  1                                               # loop moyenne jmin
            datap[14] =  1                                               # loop moyenne jmax
            datap[15] =  1 - numcellfic*inck                             # loop moyenne kmin
            datap[16] =  datap[8] -2*ific*inck + numcellfic*inck         # loop moyenne kmax
            #print datap

            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])

            ##modifie le moeud param_int de l'arbre tmy (issue de subzone) pour la fonction initNuma
            param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS
            param_int[1][0] = dim_my[1]               #nijk
            param_int[1][1] = dim_my[2]
            param_int[1][2] = dim_my[3]-1
            param_int[1][3] = datap[9]
            param_int[1][4] = datap[10]
            param_int[1][20]= 1                     #ijkv
            param_int[1][21]= 1                    
            param_int[1][22]= dim_my[3]-1 -2*ific*inck
            #
            #
	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])
            b[0][2].append(zp)

    elif (dir == 'ik'):
        for z in zones:
            #
            datap = numpy.empty((17), numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if (dim[3] == 2): inck =  0
            #
            zp = T.subzone(z, (1,1,1), (1,-1,1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)
            #
            dim_tr = Internal.getZoneDim(zp)
            dim_my = [ 0, 1, dim_tr[1], 1 ]
            #print dim_my
            datap[0]  = 2                                                # nbr direction homogene
            datap[1]  = 2                                                # dir non homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1])*(dim_my[2]-1)*(dim_my[3])        # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = (dim[1]-1  -2*ific)*(dim[3]-1  -2*ific*inck)     # nbre cellule homogene
            datap[6]  = 0                                                # nbre cellule i pour calcul adress
            datap[7]  = 1                                                # nbre cellule j
            datap[8]  = 0                                                # nbre cellule k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if (dim[3] == 2): datap[10] =  0
            datap[11] =  1                                               # loop moyenne imin
            datap[12] =  1                                               # loop moyenne imax
            datap[13] =  1 - numcellfic                                  # loop moyenne jmin
            datap[14] =  datap[7] -2*ific + numcellfic                   # loop moyenne jmax
            datap[15] =  1                                               # loop moyenne kmin
            datap[16] =  1                                               # loop moyenne kmax
            #print datap

            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])

            ##modifie le moeud param_int de l'arbre tmy (issue de subzone) pour la fonction initNuma
            param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS
            param_int[1][0] = dim_my[1]               #nijk
            param_int[1][1] = dim_my[2]-1
            param_int[1][2] = dim_my[3]
            param_int[1][3] = datap[9]
            param_int[1][4] = datap[10]
            param_int[1][20]= 1                     #ijkv
            param_int[1][21]= dim_my[2]-1 -2*ific                    
            param_int[1][22]= 1
            #
            #
	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])
            b[0][2].append(zp)

    elif (dir == 'jk'):
        for z in zones:
            #
            datap = numpy.empty((17), numpy.int32)
            #
            dim   = Internal.getZoneDim(z)
   	    inck = 1
            if (dim[3] == 2): inck =  0
            #
            zp = T.subzone(z, (1,1,1), (-1,1,1)) ; zp[0] = z[0]
            C._extractVars(zp, vars0)
	    for var in varmy: zp = C.initVars(zp, var, 0.)
            #
            dim_tr = Internal.getZoneDim(zp)
            dim_my = [ 0, dim_tr[1], 1, 1 ]
            #print dim_my
            datap[0]  = 2                                                # nbr direction homogene
            datap[1]  = 1                                                # dir non homogne
    	    datap[2]  = nsamples                                         # nbre echantillon
    	    datap[3]  = (dim_my[1]-1)*(dim_my[2])*(dim_my[3])            # nbre d'element par champ
    	    datap[4]  = len( varmy )                                     # nbre champ statistique
            datap[5]  = (dim[2]-1  -2*ific)*(dim[3]-1  -2*ific*inck)     # nbre cellule homogene
            datap[6]  = 1                                                # nbre cellule i pour calcul adress
            datap[7]  = 0                                                # nbre cellule j
            datap[8]  = 0                                                # nbre cellule k
            datap[9]  =  ific                                            # ific: a adapter  si DF
            datap[10] =  ific                                            # kfic: a adapter  si DF
            if (dim_my[3] == 2): datap[10] =  0
            datap[11] =  1 - numcellfic                                  # loop moyenne jmin
            datap[12] =  datap[6] -2*ific + numcellfic                   # loop moyenne jmax
            datap[13] =  1                                               # loop moyenne imin
            datap[14] =  1                                               # loop moyenne imax
            datap[15] =  1                                               # loop moyenne kmin
            datap[16] =  1                                               # loop moyenne kmax
            #print datap

            param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
            if param_int is None:
                raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%z[0])

            ##modifie le moeud param_int de l'arbre tmy (issue de subzone) pour la fonction initNuma
            param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS
            param_int[1][0] = dim_my[1]-1             #nijk
            param_int[1][1] = dim_my[2]  
            param_int[1][2] = dim_my[3]
            param_int[1][3] = datap[9]
            param_int[1][4] = datap[10]
            param_int[1][20]= dim_my[1]-1 -2*ific   #ijkv
            param_int[1][21]= 1
            param_int[1][22]= 1
            #
            #
	    zp[2].append([DataNodeName,datap,[],'UserDefinedData_t'])
            b[0][2].append(zp)

    else: raise ValueError("createStatNodes: not a valid direction.")

    Internal._rmNodesByType(b,'ZoneBC_t')
    Internal._rmNodesByType(b,'ZoneGridConnectivity_t')
    _compact(tmy, fields=varmy)

    return tmy

#==============================================================================
# compute statistique in place
#==============================================================================
def _computeStats(t, tmy, metrics):
    zones    = Internal.getZones(t)
    zones_my = Internal.getZones(tmy)
    fasts.computePT_my(zones, zones_my, metrics)
    return None

#==============================================================================
# compute statistique in place
#==============================================================================
def initStats(filename):

    tmy = C.convertFile2PyTree(filename)
    sol = Internal.getNodesFromName3(tmy, 'FlowSolution#Centers')
    var = Internal.getNodesFromType1(sol[0] , 'DataArray_t')

    varmy=[]
    for v in var: varmy.append('centers:'+v[0])

    _compact(tmy, fields=varmy)

    return tmy

#==============================================================================
# compute enstropy et TKE in place
#==============================================================================
def _computeEnstrophy(t, metrics, time):
    global FIRST_IT, HOOK

    zones = Internal.getZones(t)
    dtloc = Internal.getNodeFromName3(t, '.Solver#dtloc')  # noeud
    dtloc = Internal.getValue(dtloc)                       # tab numpy

    # Cree des tableaux temporaires de travail (wiggle, coe, drodm, lok, iskip_lu)
    if HOOK is None: HOOK = FastI.createWorkArrays__(zones, dtloc, FIRST_IT)

    (enstrophie, tke) = fasts.computePT_enstrophy(zones,  metrics, HOOK )

    return (enstrophie, tke)

#==============================================================================
# Post: compute variable in place
#==============================================================================
def _computeVariables(t, metrics, varlist, order=2):
    global FIRST_IT, HOOK

    if   isinstance( varlist, basestring): vars = [varlist]
    elif isinstance( varlist, list      ): vars = varlist
    else: raise ValueError("_computeVariables: last argument must be a string or list of strings.")

    lcompact_Q    = False
    lcompact_Enst = False
    lcompact_drodt= False
    lcompact_Rotx = False

    flag = 0
    var_loc = []

    for var in vars:
       #print 'var=',var
       if (var == 'QCriterion'):  
          flag += 1
          var_loc.append('QCriterion')
       elif (var == 'QpCriterion'):  
          flag += 2
          var_loc.append('QpCriterion')
       elif (var == 'Enstrophy'): 
          flag += 10
          var_loc.append('Enstrophy')
       elif (var == 'RotX'): 
          flag += 100
          var_loc.append('RotX')
       elif (var == 'dDensitydt'): 
          flag += 1000
          var_loc.append('dDensitydt')

    #print flag,var_loc
    ##verifie si les noeux existe dans l'arbre
    zones = Internal.getZones(t)
    for z in zones:
        dim = Internal.getZoneDim(z)
        size = (dim[1]-1)*(dim[2]-1)*(dim[3]-1)
        solution = Internal.getNodeFromName1(z, 'FlowSolution#Centers')
     
        for var in var_loc:
            node = Internal.getNodeFromName1(solution, var)
            if node is None:
                 if(var=='QCriterion' or var=='QpCriterion'): lcompact_Q     = True
                 if(var=='RotX'      )                      : lcompact_Rotx  = True
                 if(var=='Enstrophy' )                      : lcompact_Enst  = True
                 if(var=='dDensity/dt')                     : lcompact_drodt = True
                 tmp = numpy.ones( (dim[1]-1,dim[2]-1,dim[3]-1) , dtype=numpy.float64)
                 Internal.createChild(solution, var, 'DataArray_t', tmp)

    if (var_loc != []):
       if (lcompact_Q):     _compact(t, fields=['centers:QCriterion'])
       if (lcompact_Enst) : _compact(t, fields=['centers:Enstrophy' ])
       if (lcompact_Rotx) : _compact(t, fields=['centers:RotX' ])
       if (lcompact_drodt): _compact(t, fields=['centers:dDensitydt' ])

       dtloc = Internal.getNodeFromName3(t , '.Solver#dtloc')  # noeud
       dtloc = Internal.getValue(dtloc)                       # tab numpy

       # Cree des tableaux temporaires de travail (wiggle, coe, drodm, lok, iskip_lu)
       if HOOK is None: HOOK = FastI.createWorkArrays__(zones, dtloc, FIRST_IT)
       fasts.computePT_variables(zones,  metrics, HOOK, flag, order)
    return None

#==============================================================================
# Post: compute gradient in place
#==============================================================================
def _computeGrad(t, metrics, varlist, order=2):
    global FIRST_IT, HOOK

    if isinstance(varlist, basestring): vars = [varlist]
    elif isinstance(varlist, list    ): vars = varlist
    else: raise ValueError("_computeGrad: last argument must be a string or list of strings.")

    var_loc = []
    vargrad = []

    ##verifie si les noeuds existent dans l'arbre
    zones = Internal.getZones(t)
    for z in zones:
        #
        dim   = Internal.getZoneDim(z)
        size = (dim[1]-1)*(dim[2]-1)*(dim[3]-1)
        solution = Internal.getNodeFromName1(z, 'FlowSolution#Centers')
     
        for var in vars:
            node = Internal.getNodeFromName1(solution, var)
            #verifie si la variable existe dans l'arbre
            if node is None:
               print "no", var, "in tree: gradient is not computed."
            else:
               var_loc.append(var)
               lcompact = False
               vargrad.append('gradx'+var)
               vargrad.append('grady'+var)
               vargrad.append('gradz'+var)
               #creation noeuds gradient si necessaire
               for grad in ['gradx'+var, 'grady'+var, 'gradz'+var]:
                   node_grad = Internal.getNodeFromName1(solution, grad)
                   if node_grad is None:
                      #tmp = numpy.empty( (dim[1]-1,dim[2]-1,dim[3]-1) , dtype=numpy.float64)
                      tmp = numpy.ones( (dim[1]-1,dim[2]-1,dim[3]-1) , dtype=numpy.float64)
                      Internal.createChild(solution, grad, 'DataArray_t', tmp)
                      lcompact = True

               if(lcompact): _compact(t, fields=['centers:'+vargrad[0],'centers:'+vargrad[1],'centers:'+vargrad[2]])
      
    if(var_loc !=[]):
       dtloc = Internal.getNodeFromName3(t , '.Solver#dtloc')  # noeud
       dtloc = Internal.getValue(dtloc)                       # tab numpy

       # Cree des tableaux temporaires de travail (wiggle, coe, drodm, lok, iskip_lu)
       if HOOK is None: HOOK = FastI.createWorkArrays__(zones, dtloc, FIRST_IT)
       fasts.computePT_gradient(zones,  metrics, var_loc, vargrad, HOOK, order)
    return None

#==============================================================================
# Display
#==============================================================================
def display_temporal_criteria(t, metrics, nitrun, format=None):
    zones        = Internal.getZones(t)
    dtloc        = Internal.getNodeFromName3(t, '.Solver#dtloc')
    dtloc_numpy  = Internal.getValue(dtloc)
    nssiter      = int(dtloc_numpy[0])
    nzones	 = len(zones)
 
    #a = Internal.getNodeFromName2(zones[0], 'model')
    #model = Internal.getValue(a)
    #neq = 5
    #if (model == 'nsspalart' or model =='NSTurbulent'): neq = 6
    neq_max = 6
   
    cvg_numpy = numpy.empty((nzones,2*neq_max) , dtype=numpy.float64)
    # sortie sur stdout "simple precision" 
    if format is None: lft = 1
    # sortie sur stdout "double precision" 
    elif format == "double": lft = 0
    # sortie sur Fichier Fortran binaire
    elif format == "flush": lft = -1
    # sortie sur Fichier Fortran binaire
    elif format == "ascii": lft = -2
    # enregistrement dans l'arbre uniquement
    elif format == "store": lft = 3
    fasts.display_ss_iteration( zones, metrics, cvg_numpy, nitrun, 
                                nssiter, lft)

    return None

#==============================================================================
# Construit les donnees compactees pour traiter les BC
#==============================================================================
def _BCcompact(t):
    # Data for each Base
    bases = Internal.getNodesFromType1(t, 'CGNSBase_t')

    #dimensionnememnt tableau
    size_param_int =[]
    size_param_real=[]
    for b in bases:
        zones = Internal.getZones(b)
        nzones=len(zones)
        for z in zones:

            bcs   = Internal.getNodesFromType2(z, 'BC_t')
            Nb_bc = len(bcs)
            size_int  = 1 +  Nb_bc*2 + 9*Nb_bc
            size_real = 0
            for bc in bcs:
                bcdata  = Internal.getNodesFromType3(bc, 'DataArray_t')
                Nb_data = len(bcdata)
                for data in bcdata:
                   size = numpy.shape(data[1])
                   c = 1
                   for s in size: c=c*s
                   # a sortie de la boucle: voir fastP
                   size_int  = size_int  + Nb_data

                   size_real = size_real + c

            # zone ownData (generated)
            o = Internal.getNodeFromName1( z, '.Solver#ownData')

            #on concatene les donnes BC dans param_int et param_real
            param_int = Internal.getNodeFromName1( o, 'Parameter_int')
            size = numpy.shape(param_int[1])
            c = 1
            for s in size: c=c*s
            size_param_int.append(c)

            datap = numpy.zeros(size_int+c, numpy.int32)
            datap[0:c]   = param_int[1][0:c]
            param_int[1] = datap

            param_real = Internal.getNodeFromName1( o, 'Parameter_real')
            size = numpy.shape(param_real[1])
            c = 1
            for s in size: c=c*s
            size_param_real.append(c)

            if size_real != 0:
                datap = numpy.zeros(size_real+c, numpy.float64)
                datap[0:c]    = param_real[1][0:c]
                param_real[1] = datap


    #Init param BC
    no = 0
    for b in bases:
        zones = Internal.getNodesFromType1(b, 'Zone_t')
        nzones=len(zones)
        for z in zones:

            o         = Internal.getNodeFromName1( z, '.Solver#ownData')
            bcs       = Internal.getNodesFromType2(z, 'BC_t')
            param_int = Internal.getNodeFromName1( o, 'Parameter_int')[1]
            param_real= Internal.getNodeFromName1( o, 'Parameter_real')[1]

            Nb_bc = len(bcs)

            #pt_bcs_int = 64
            #pt_bcs_real= 37
            pt_bcs_int =  size_param_int[no]
            pt_bcs_real=  size_param_real[no] 
            param_int[ pt_bcs_int ] = Nb_bc
            i         = 1
            size_int  = 1 + 2*Nb_bc  # shift pour nb_bc et pointeur BC_int et BC_real
            size_real = 0
            no       += 1
            for bc in bcs:
                param_int[ pt_bcs_int +i       ] = size_int  + pt_bcs_int
                param_int[ pt_bcs_int +i +Nb_bc] = size_real + pt_bcs_real
                pt_bc                            =  param_int[ pt_bcs_int +i ] 

                param_int[pt_bc] = FastI.tagBC( Internal.getValue(bc) )

                Ptrange = Internal.getNodeFromType1( bc , 'IndexRange_t')
                indrange= Internal.getValue(Ptrange)
                ind_bc  = numpy.zeros(6, numpy.int32)
                ind_bc[0] = indrange[0][0]
                ind_bc[1] = indrange[0][1]
                ind_bc[2] = indrange[1][0]
                ind_bc[3] = indrange[1][1]
                ind_bc[4] = indrange[2][0]
                ind_bc[5] = indrange[2][1]
                fasts.PygetRange( ind_bc,  param_int, pt_bc+ 1)
                bcdata  = Internal.getNodesFromType3(bc, 'DataArray_t')
                Nb_data = len(bcdata)
                param_int[pt_bc + 8] = Nb_data
                j = 1
                ctot = 0
                for data in bcdata:
                   size = numpy.shape(data[1])
                   c = 1
                   for s in size: c=c*s
                   ctot = ctot +c
                   param_int[pt_bc + 8 + j ] = ctot
                   deb = pt_bcs_real+size_real
                   param_real[ deb:deb+c ]= data[1].flat[0:c]
                   data[1]                = param_real[ deb:deb+c ] # wrong shape?
                   size_real = size_real + c
                   j = j+1

                size_int  = size_int  + 9 + Nb_data
                i         = i + 1

#==============================================================================
# Construit les datas possedees par FastS
#==============================================================================
def _buildOwnData(t):
    # Data for each Base
    bases = Internal.getNodesFromType1(t, 'CGNSBase_t')

    #init time et No iteration
    it0 =0; temps=0.
    first = Internal.getNodeFromName1(t, 'Iteration')
    if first is not None: it0 = Internal.getValue(first)
    first = Internal.getNodeFromName1(t, 'Time')
    if first is not None: temps = Internal.getValue(first)

    # Ecriture d un vecteur contenant le niveau en temps de chaque zone
    # Determination du niveau en temps le plus grand 
    
    levelg=[]; leveld=[]; val=1; i=0
    veclevel = []   
    for b in bases:
           zones = Internal.getNodesFromType1(b, 'Zone_t')
           for z in zones:
                d = Internal.getNodeFromName1(z, '.Solver#define')
                if d is not None:
                    a = Internal.getNodeFromName1(d, 'niveaux_temps')
                    if a is not None: val = Internal.getValue(a)
                veclevel.append(val)
                i += 1
    
    maxlevel = max(veclevel)
    
    levelg = numpy.roll(veclevel,1)
    levelg[0] = 0

    leveld = numpy.roll(veclevel,-1)
    leveld[i-1] = 0
                                                            
    # Available keys for bases and zones
    # 0: requires and int, 1: requires a float, 2: requires any string, 
    # 3: requires array/list of ints, 4: requires array/list of floats,
    # []: requires given strings
    keys4Base = {
    'temporal_scheme':['explicit', 'implicit', 'implicit_local'], 
    'ss_iteration':0,
    'rk':0, 
    'modulo_verif':0,
    'exp_local':0,
    'time_begin_ale':1,
    'omp_mode':0
    }
    keys4Zone = {
    'scheme':['ausmpred', 'senseur', 'roe_min', 'roe', 'roe_nul', 'roe_kap'],
    'motion':['none', 'rigid', 'deformation'],
    'rotation':4,
    'time_step':1,
    'io_thread':0,
    'sgsmodel': ['smsm','Miles'],
    'cache_blocking_I':0,
    'cache_blocking_J':0,
    'cache_blocking_K':0,
    'time_step_nature':['local', 'global'],
    'ssdom_IJK':3,
    'epsi_newton':1,
    'inj1_newton_tol':1,
    'inj1_newton_nit':0,
    'cfl':1, 
    'niveaux_temps':0, 
    'psiroe':1, 
    'prandtltb':1, 
    'sfd':0, 
    'sfd_chi':1, 
    'sfd_delta':1, 
    'sfd_init_iter':0, 
    'slope':["o1", "o3", "minmod"],
    'DES':["zdes1", "zdes1_w", "zdes2", "zdes2_w", "zdes3"],
    'snear': 1 # ignored
    }

    for b in bases:
        # Base define data
        d = Internal.getNodeFromName1(b, '.Solver#define')

        # - defaults -
        temporal_scheme = "implicit"
        ss_iteration    = 30
        rk              = 3
        modulo_verif    = 200
        restart_fields  = 1
        exploc          = 0
        t_init_ale      = temps

        timelevel_motion= 0
        timelevel_target= 0
        timelevel_prfile= 0

        if d is not None:
            FastI.checkKeys(d, keys4Base)
            a = Internal.getNodeFromName1(d, 'temporal_scheme')
            if a is not None: temporal_scheme = Internal.getValue(a)
            a = Internal.getNodeFromName1(d, 'ss_iteration')
            if a is not None: ss_iteration = Internal.getValue(a)
            if temporal_scheme == "implicit_local": modulo_verif = 7
            a = Internal.getNodeFromName1(d, 'modulo_verif')
            if a is not None: modulo_verif = Internal.getValue(a)
            a = Internal.getNodeFromName1(d, 'restart_fields')
            if a is not None: restart_fields = Internal.getValue(a)
            a = Internal.getNodeFromName1(d, 'rk')
            if a is not None: rk = Internal.getValue(a)
            if temporal_scheme == "implicit": rk=3
            a = Internal.getNodeFromName1(d, 'exp_local')
            if a is not None: exploc = Internal.getValue(a)
            if temporal_scheme == "implicit": exploc=0
            a = Internal.getNodeFromName1(d, 'it_exp_local')         
            if a is not None: itexploc = Internal.getValue(a)
            a = Internal.getNodeFromName1(d, 'time_begin_ale')
            if a is not None: t_init_ale = Internal.getValue(a)  

        a = Internal.getNodeFromName1(t, 'TimeLevelMotion')
        if a is not None: timelevel_motion = Internal.getValue(a)
        a = Internal.getNodeFromName1(t, 'TimeLevelTarget')
        if a is not None: timelevel_target = Internal.getValue(a)
          
        # Base ownData (generated)
        o = Internal.createUniqueChild(b, '.Solver#ownData', 
                                       'UserDefinedData_t')
        if temporal_scheme == "explicit": nssiter = 3
        elif temporal_scheme == "implicit": nssiter = ss_iteration+1
        elif temporal_scheme == "implicit_local": nssiter = ss_iteration+1
        else: print 'Warning: FastS: invalid value %s for key temporal_scheme.'%temporal_scheme
        try: ss_iteration = int(ss_iteration)
        except: print 'Warning: FastS: invalid value %s for key ss_iteration.'%ss_iteration
        try: modulo_verif = int(modulo_verif)
        except: print 'Warning: FastS: invalid value %s for key modulo_verif.'%modulo_verif
        if (rk == 1 and exploc==0 and temporal_scheme == "explicit"): nssiter = 1 # explicit global
        if (rk == 2 and exploc==0 and temporal_scheme == "explicit"): nssiter = 2 # explicit global
        if (rk == 3 and exploc==0 and temporal_scheme == "explicit"): nssiter = 3 # explicit global
        if (exploc == 1 and temporal_scheme == "explicit"): nssiter = rk*maxlevel # explicit local
        if (exploc == 2 and temporal_scheme == "explicit"): itexploc = 4
        else: itexploc=0
        dtdim = nssiter + 7
        datap = numpy.empty((dtdim), numpy.int32) 
        datap[0] = nssiter 
        datap[1] = modulo_verif
        datap[2] = restart_fields-1
        datap[3] = timelevel_motion
        datap[4] = timelevel_target 
        datap[5] = timelevel_prfile 
        datap[6:dtdim-1] = 1
        datap[dtdim-1] = rk
        Internal.createUniqueChild(o, '.Solver#dtloc', 'DataArray_t', datap)

    # Data for each zone
    bases = Internal.getNodesFromType2(t, 'CGNSBase_t')
    
    i=0
    for b in bases:
        zones = Internal.getNodesFromType1(b, 'Zone_t')
        nzones=len(zones)
        for z in zones:

            # zone ownData (generated)
            o = Internal.createUniqueChild(z, '.Solver#ownData', 
                                           'UserDefinedData_t')

            # - defaults -
            model    = "Euler"
            sgsmodel = "Miles"
            des      = "none"
            temporal_scheme = "implicit"
            scheme  = "ausmpred"
            slope   = "o3"
            motion = "none"
            filtrage = "off"
            io_th = 0
            cacheblckI = 2048
            cacheblckJ = 3
            cacheblckK = 2
            dtnature = "global"
            dtc      = -0.000001
            epsi_newton  = 0.1 
            psiroe       = 0.1
            cfl          = 1.
            rotation     = [ 0.,0.,0., 0.,0.,0.,0.,0.]
            ssdom_IJK    = [240,20,900]
            sfd          = 0
            sfd_chi      = 0.
            sfd_delta    = 1.e15
            sfd_init_iter= 1
            nit_inflow   = 10
            epsi_inflow  = 1.e-5
            
            a = Internal.getNodeFromName2(z, 'GoverningEquations')
            if a is not None: model = Internal.getValue(a)
            else:
                a = Internal.getNodeFromName2(b, 'GoverningEquations')
                if a is not None: model = Internal.getValue(a)
            a = Internal.getNodeFromName2(z, 'DES')
            if a is not None: des = Internal.getValue(a)
            else:
                a = Internal.getNodeFromName2(z, 'des')
                if a is not None: des = Internal.getValue(a)
                else:
                    a = Internal.getNodeFromName2(b, 'DES')
                    if a is not None: des = Internal.getValue(a)
                    else:
                        a = Internal.getNodeFromName2(b, 'des')
                        if a is not None: des = Internal.getValue(a)
            ref = None
            a = Internal.getNodeFromName1(z, 'ReferenceState')
            if a is not None: ref = a
            else:
                a = Internal.getNodeFromName1(b, 'ReferenceState')
                if a is not None: ref = a
            adim = None
            if ref is not None: 
              adim      = C.getState(ref)
              prandtltb = adim[18]        #prandtl laminar 
            else: 
              print 'FastS: Warning: can not find a refState.'
              print 'FastS: Warning: Prandtl turb by default= 1.'
              prandtltb = 1.

            d = Internal.getNodeFromName1(z, '.Solver#define')
            if d is not None:
                FastI.checkKeys(d, keys4Zone)
                a = Internal.getNodeFromName2(b, 'temporal_scheme')
                if a is not None: temporal_scheme = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'scheme')
                if a is not None: scheme = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'slope')
                if a is not None: slope  = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'motion')
                if a is not None: motion = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'rotation')
                if a is not None: rotation = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'filtrage')
                if a is not None: filtrage = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'io_thread')
                if a is not None: io_th = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'cache_blocking_I')
                if a is not None: cacheblckI = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'cache_blocking_J')
                if a is not None: cacheblckJ = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'cache_blocking_K')
                if a is not None: cacheblckK = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'sgsmodel')
                if a is not None: sgsmodel = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'time_step_nature')
                if a is not None: dtnature = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'time_step')
                if a is not None: dtc = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'inj1_newton_tol')
                if a is not None: epsi_inflow = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'inj1_newton_nit')
                if a is not None: nit_inflow = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'epsi_newton')
                if a is not None: epsi_newton = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'cfl')
                if a is not None: cfl = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'ssdom_IJK')
                if a is not None: ssdom_IJK = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'prandtltb')
                if a is not None: prandtltb = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'psiroe')
                if a is not None: psiroe = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'sfd')
                if a is not None: sfd = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'sfd_chi')
                if a is not None: sfd_chi = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'sfd_delta')
                if a is not None: sfd_delta = Internal.getValue(a)
                a = Internal.getNodeFromName1(d, 'sfd_init_iter')
                if a is not None: sfd_init_iter = Internal.getValue(a)
               
            iflow  = 1
            ides   = 0; idist = 1; ispax = 2; izgris = 0; iprod = 0;
            azgris = 0.01; addes = 0.2; ratiom = 10000.

            if model == "Euler" or model == "euler":
                iflow  = 1
            elif  model == "NSLaminar" or model == "nslaminar": 
                iflow = 2
            elif model == "NSLes" or model == "nsles": 
                iflow = 2
            elif model == "nsspalart" or model == "NSTurbulent": 
                iflow = 3
                if des != 'none':
                    if   des == "ZDES1" or des == "zdes1": ides = 1
                    elif des == "ZDES1_w" or des == "zdes1_w": ides = 2
                    elif des == "ZDES2" or des == "zdes2": ides = 3
                    elif des == "ZDES2_w" or des == "zdes2_w": ides = 4
                    elif des == "ZDES3" or des == "zdes3": ides = 5
            else:
                 print 'Warning: FastS: model %s is invalid.'%model

            iles = 0
            if sgsmodel == 'smsm': iles = 1


            size_ssdom_I = 1000000
            size_ssdom_J = 1000000
            size_ssdom_K = 1000000

            if temporal_scheme == "explicit": itypcp = 2
            else: itypcp = 1

            if temporal_scheme == "implicit_local": 
                itypcp = 1
                ssdom_IJK = [80,15,80]
                a = Internal.getNodeFromName1(d, 'ssdom_IJK')
                if a is not None: 
                    ssdom_IJK = Internal.getValue(a)
                size_ssdom_I = ssdom_IJK[0]
                size_ssdom_J = ssdom_IJK[1]
                size_ssdom_K = ssdom_IJK[2]

            if   slope == "o1"    : islope = 1
            elif slope == "o3"    : islope = 2
            elif slope == "minmod": islope = 3

            kfludom = 1
            if   scheme == "ausmpred" : kfludom = 1
            elif scheme == "senseur"  : kfludom = 2
            elif scheme == "dfcentre6": kfludom = 3
            elif scheme == "roe_kap"  :
              kfludom = 5
              islope  = 2
            elif scheme == "roe_min": 
              kfludom = 5
              islope  = 3
            elif scheme == "roe_nul": 
              kfludom = 5
              islope  = 1
            elif scheme == "roe": 
              kfludom = 5
            else: print 'Warning: FastS: scheme %s is invalid.'%scheme

            lale   = 0; size_ale =0;
            if    motion == "none"       : lale = 0
            elif  motion == "rigid"      : lale = 1; size_ale = 11
            elif  motion == "deformation": lale = 2
            else: print 'Warning: FastS: motion %s is invalid.'%motion

            iflagflt = 0
            if filtrage == "on": iflagflt = 1

            dtloc = 0
            if dtnature == "global": dtloc = 0
            elif dtnature == "local": dtloc = 1
            else: print 'Warning: FastS: time_step_nature %s is invalid.'%dtnature

            # creation noeud parametre integer
            # Determination de levelg et leveld             

            datap = numpy.empty(66, numpy.int32)
            datap[0:25]= -1
            datap[25]  = 0     # zone 3D curvi par defaut
            datap[26]  = 0     # Vent 3D par defaut
            datap[27]  = iflow
            datap[28]  = iles
            datap[29]  = itypcp
            datap[30]  = size_ssdom_I
            datap[31]  = size_ssdom_J
            datap[32]  = size_ssdom_K
            datap[33]  = kfludom
            datap[34]  = lale
            datap[35]  = iflagflt
            datap[36:45]= -1
            datap[45]  = io_th    
            datap[46]  = dtloc
            datap[47]  = ides  
            datap[48]  = idist 
            datap[49]  = ispax
            datap[50]  = izgris
            datap[51]  = iprod
            datap[52]  = rk
            datap[53]  = veclevel[i]
            datap[54]  = exploc
            datap[55]  = itexploc
            datap[56]  = levelg[i]
            datap[57]  = leveld[i]
            datap[58]  = nssiter
            datap[59]  = cacheblckI
            datap[60]  = cacheblckJ
            datap[61]  = cacheblckK
            datap[62]  = sfd
            datap[63]  = sfd_init_iter
            datap[64]  = islope
            datap[65]  = nit_inflow

            i += 1
         
            Internal.createUniqueChild(o, 'Parameter_int', 'DataArray_t', datap)
            
            # creation noeud parametre real 
            #size_real = 23 +  size_ale   
            size_real = 39
            datap = numpy.zeros(size_real, numpy.float64)
            if dtc < 0: 
                print 'Warning: time_step set to default value (0.0001).'
                dtc = 0.0001
            datap[0] = dtc
            
            if adim is not None:
                pinf    = adim[5]
                ainf    = math.sqrt(adim[11]*pinf/adim[0])
                datap[ 1]=  adim[11]    # gamma
                datap[ 2]=  adim[ 7]    # cv
                datap[ 3]=  adim[ 0]    # roinf
                datap[ 4]=  adim[ 5]    # pinf
                datap[ 5]=  math.sqrt(adim[1]**2 + adim[2]**2 + adim[3]**2) / adim[0] # uinf
                datap[ 6]=  adim[6]      # Tinf
                datap[ 7]=  adim[ 8]     # Minf
                datap[ 8]=  adim[14]     # roNutildeinf
                datap[ 9]=  adim[ 9]     # Re
                datap[10]=  adim[18]     # Pr
                datap[11]=  adim[15]     # Mus
                datap[12]=  adim[17]     # Ts (sutherland)
                datap[13]=  adim[16]     # Cs (sutherland)
                datap[19]= adim[1]/adim[0] # Vx inf
                datap[20]= adim[2]/adim[0] # Vy inf
                datap[21]= adim[3]/adim[0] # Vz inf
            else: datap[1:14] = 0. ; datap[19:22] = 0.

            datap[14]=  epsi_newton
            datap[15]=  cfl        
            datap[16]=  azgris
            datap[17]=  addes
            datap[18]=  ratiom
            datap[22]=  temps
 
            if lale ==1: 
                datap[23:31]=  rotation[0:8]
                datap[29]   = datap[29]*2.  *math.pi
                datap[30]   = datap[30]/180.*math.pi
                datap[31]   = t_init_ale
                datap[32]   = 0.     #teta
                datap[33]   = 0.     #tetap

            datap[34]=  psiroe
            datap[35]=  sfd_chi
            datap[36]=  sfd_delta
            datap[37]=  epsi_inflow
            datap[38]=  prandtltb

            Internal.createUniqueChild(o, 'Parameter_real', 'DataArray_t', datap)

            # More
            Internal.createUniqueChild(o, 'CFL_minmaxmoy', 'DataArray_t', [0.,0.,0.])
            Internal.createUniqueChild(o, 'type_zone'    , 'DataArray_t',  0)
            Internal.createUniqueChild(o, 'model', 'DataArray_t', model)
            Internal.createUniqueChild(o, 'temporal_scheme', 'DataArray_t', temporal_scheme)
    return None


#==============================================================================
# init veloty (ALE)
#==============================================================================
def _computeVelocityAle(t, metrics):
    global FIRST_IT, HOOK


    dtloc = Internal.getNodeFromName3(t, '.Solver#dtloc')  # noeud
    dtloc = Internal.getValue(dtloc)                       # tab numpy

    zones = Internal.getZones(t)
    # Cree des tableaux temporaires de travail (wiggle, coe, drodm, lok, iskip_lu)
    f_it = FIRST_IT
    if HOOK is None: HOOK = FastI.createWorkArrays__(zones, dtloc, f_it ); FIRST_IT = f_it;
    
    fasts.computePT_velocity_ale(zones,  metrics, HOOK)
    return None

#==============================================================================
# move mesh (ALE)
#==============================================================================
def _movegrid(t):
    zones = Internal.getZones(t)
    fasts._movegrid(zones)
    return None

#==============================================================================
# loi horaire (ALE) 
#==============================================================================
def _motionlaw(t, teta, tetap):
    zones = Internal.getZones(t)
    fasts._motionlaw(zones, teta, tetap)
    return None

#==============================================================================
# Convergence
#==============================================================================
def createConvergenceHistory(t, nrec):
    varsR   = [ 'RSD_L2','RSD_oo']
    bases   = Internal.getNodesFromType1(t, 'CGNSBase_t')
    curIt   = 0
    for b in bases:
       Internal.createUniqueChild(b, 'GlobalConvergenceHistory',
                                  'ConvergenceHistory_t', value=curIt)
    zones  = Internal.getZones(t)
    neq = 5
    a = Internal.getNodeFromName3(t, 'GoverningEquations')
    if a is not None:
       model = Internal.getValue(a)
       if model == 'nsspalart' or model =='NSTurbulent': neq = 6
    for z in zones:
        c = Internal.createUniqueChild(z, 'ZoneConvergenceHistory',
                                       'ConvergenceHistory_t', value=curIt) 
        tmp = numpy.empty((nrec), numpy.int32)
        Internal.createChild(c, 'IterationNumber', 'DataArray_t', tmp)
        for var in varsR:
            tmp = numpy.empty((nrec*neq), numpy.float64)
            Internal.createChild(c, var ,'DataArray_t', tmp)
    return None

#==============================================================================
# extraction des residus du Pytree dans fichier tecplot ascii
# ==============================================================================
def extractConvergenceHistory(t, fileout):
    zones = Internal.getZonePaths(t)
    it = []; RSD_L2 = []; RSD_oo = []; a = []
    nd = 0
    FileCvgName = fileout
    FileCvg = open(FileCvgName,'w')
    for i in zones:
        node = Internal.getNodeFromPath(t, i+'/ZoneConvergenceHistory')
        lastRec = node[1][0]
        #it = C.convertPyTree2Array(i+"/ZoneConvergenceHistory/IterationNumber", t)
        #a  = C.convertPyTree2Array(i+"/ZoneConvergenceHistory/RSD_L2", t)
        it = Internal.getNodeFromPath(t,i+"/ZoneConvergenceHistory/IterationNumber")
        a = Internal.getNodeFromPath(t, i+"/ZoneConvergenceHistory/RSD_L2")
        nrec = it[1].size
        neq = a[1].size/nrec
        RSD_L2 = numpy.reshape(a[1],(neq,nrec),order='F')
        #a = C.convertPyTree2Array(i+"/ZoneConvergenceHistory/RSD_oo", t)
        a = Internal.getNodeFromPath(t, i+"/ZoneConvergenceHistory/RSD_oo")
        RSD_oo = numpy.reshape(a[1],(neq,nrec),order='F')
        a='"'
        if neq == 5: 
            var="VARIABLES = it RO_l2 ROU_l2 ROV_l2 ROW_l2 ROE_l2 ROoo ROUoo ROVoo ROWoo ROEoo\n"
        if neq == 6: 
            var="VARIABLES = it RO_l2 ROU_l2 ROV_l2 ROW_l2 ROE_l2 NUT_l2 ROoo ROUoo ROVoo ROWoo ROEoo NUToo\n"
        if nd == 0: FileCvg.write("%s"%(var)) 
        nd =+ 1
        FileCvg.write("ZONE T=%sbloc %s %s I=%d F=POINT\n"%(a,i,a,lastRec))
        c = it[1]
        for l in xrange(lastRec):
           a  = ""
           for k in xrange(neq): a = a+"{0:7f} ".format(RSD_L2[(k,l)])
           for k in xrange(neq): a = a+"{0:7f} ".format(RSD_oo[(k,l)])
           FileCvg.write('%s %s\n'%(1+c[l],a))
    FileCvg.close()

#==============================================================================
# Cree un arbre Stress pour calcul effort
# IN: t: tree
# IN: BC: list des BC concernees ['BCWall', BCFarfield',...]
# OUT: return arbre stress
#==============================================================================
#def createStressNodes(t, BC=['BCWall']):
def createStressNodes(t, BC=None, window=None):

    import Converter.GhostCells as Ghost
    try: import Transform.PyTree as T
    except: raise ImportError("createStressNodes: requires transform module.")

    PostBaseName = 'Stress' # nom de la base POST
    vars0  = ['CoordinateX','CoordinateY','CoordinateZ']
    var    = ['Density','MomentumX','MomentumY','MomentumZ','EnergyStagnationDensity']

    teff = C.newPyTree([PostBaseName])
    b    = Internal.getNodesFromName1(teff, PostBaseName)

    zones = []
    no_z =0
    for b0 in Internal.getNodesFromType1(t,'CGNSBase_t'):
        dimbase=  b0[1][0]
        if b0[0] != PostBaseName:
           zones    = Internal.getNodesFromType1(b0, 'Zone_t')

           for z in zones:

              # gestion zone2d
              dimzone= Internal.getZoneDim(z)
              inck0 = 2
              inck1 = 1
              vargrad=['gradxVelocityX','gradyVelocityX','gradzVelocityX','gradxVelocityY','gradyVelocityY','gradzVelocityY','gradxVelocityZ','gradyVelocityZ','gradzVelocityZ','gradxTemperature','gradyTemperature','gradzTemperature', 'CoefPressure','ViscosityMolecular']
              if dimzone[3] == 2: 
                   inck0=0
                   inck1=0
                   vargrad=['gradxVelocityX','gradyVelocityX','gradxVelocityY','gradyVelocityY','gradxTemperature','gradyTemperature','CoefPressure','ViscosityMolecular']

              varc  = []
              for i in xrange(len(var)): varc.append('centers:'+var[i])
              for i in xrange(len(vargrad)): varc.append('centers:'+vargrad[i])
              
              if BC is not None:
                 bc = Internal.getNodeFromName1(z, "ZoneBC")
                 list_bc =[]
                 if (bc is not None): list_bc = bc[2]
              else:
                 list_bc =['window']
              compact = 0
              ific    = 2
              param_int = Internal.getNodeFromName2(z, 'Parameter_int')
              if (param_int is not None): ific = param_int[1][3]
              ndf =0
              for v in list_bc:
                 if BC is not None:
                   name    = Internal.getValue(v)
                 else:
                   name    = v[0]

                 #if name in BC:
                 if (BC is not None and name in BC) or (window is not None and z[0]==window[0]):
                   #print 'coucou', window, z[0], BC
                   if BC is not None:
                      ptrg = Internal.getNodeFromName(v, "PointRange")
                   else: 
                      range = numpy.zeros(6, numpy.int32 ).reshape(3,2)
                      range[0,0]= window[1]
                      range[1,0]= window[3]
                      range[2,0]= window[5]
                      range[0,1]= window[2]
                      range[1,1]= window[4]
                      range[2,1]= window[6]
                      ptrg = [ 'PointRange', range,  [], 'IndexRange_t']
                      
                   dim = Internal.getValue(ptrg)
                   #print 'ptrg',ptrg
                   print 'dim ', dim
                   compact = 1

                   #dir=0,...5
                   idir = Ghost.getDirection__(dimbase, [ptrg])
                   #print 'idir ', idir
                   inci =0
                   incj =0
                   inck =0
                   if BC is not None:
                      if  (idir==0): inci= ific
                      elif(idir==1): inci=-ific
                      elif(idir==2): incj= ific
                      elif(idir==3): incj=-ific
                      elif(idir==4): inck= inck0
                      elif(idir==5): inck=-inck0

                   ni = dim[0,1]-dim[0,0]
                   nj = dim[1,1]-dim[1,0]
                   nk = dim[2,1]-dim[2,0]
                   zp = T.subzone(z, (dim[0,0]+inci,dim[1,0]+incj,dim[2,0]+inck), (dim[0,1]+inci,dim[1,1]+incj,dim[2,1]+inck))
                   zp[0] = z[0]+'_'+v[0]
                   C._extractVars(zp, vars0)
                   c = 0
	           for v1 in varc: 
                      #print 'var name=',v1,zp[0], c
                      zp = C.initVars(zp, v1, c)
                      c=c+1
                   #print 'zone name=',zp[0]
                   #print 'dim0=',dim[0,0],dim[0,1],dim[1,0],dim[1,1],dim[2,0],dim[2,1]

                   if idir <= 1:
                     ni1      = nj
                     nj1      = nk
                     imin     = dim[0,0]
                     imax     = imin
                     jmin     = dim[1,0]-ific      #jmin
                     jmax     = dim[1,1]-ific-1    #jmax
                     kmin     = dim[2,0]-inck0     #kmin
                     kmax     = dim[2,1]-inck0-1   #kmax
                     if(idir==1 and BC is not None):
                          imin    = dim[0,0]-2*ific
                          imax    = imin
                   elif (idir <= 3):   
                     ni1      = ni
                     nj1      = nk
                     jmin     = dim[1,0]
                     jmax     = jmin
                     imin     = dim[0,0]-ific      #imin
                     imax     = dim[0,1]-ific-1    #imax
                     kmin     = dim[2,0]-inck0     #kmin
                     kmax     = dim[2,1]-inck0-1   #kmax
                     if (idir == 3 and BC is not None): 
                          jmin = dim[1,0]-2*ific
                          jmax = jmin
                   else:
                     ni1      = ni
                     nj1      = nj
                     kmin     = dim[2,0]
                     kmax     = kmin
                     imin     = dim[0,0]-ific      #imin
                     imax     = dim[0,1]-ific-1    #imax
                     jmin     = dim[1,0]-ific      #jmin
                     jmax     = dim[1,1]-ific-1    #jmax
                     if (idir == 5 and BC is not None):
                          kmin = dim[2,0]-2*inck0
                          kmax = kmin

                   param_int = Internal.getNodeFromName2(zp, 'Parameter_int')  # noeud
                   if param_int is None:
                      raise ValueError("_createStatNodes: Parameter_int is missing for zone %s."%zp[0])

                   #print 'loop',imin,imax,jmin,jmax,kmin,kmax
                   ##modifie le moeud param_int de l'arbre teffot (issue de subzone) pour la fonction initNuma
                   param_int[1]    = numpy.copy(param_int[1])    # sinon tableau partager avec param de la zone NS
                   #NIJK  pour initNuma
                   param_int[1][ 0] = ni1
                   param_int[1][ 1] = nj1
                   param_int[1][ 2] = 1
                   param_int[1][ 3] = ific
                   param_int[1][ 4] = 0
                   #IJKV  pour initNuma
                   param_int[1][20] = max(ni1-2*ific, 1)
                   param_int[1][21] = max(nj1-2*ific, 1)
                   param_int[1][22] = 1
                   param_int[1][36] = len(varc)             #NEQ    
                   param_int[1][41] = ni1*nj1               #NDIMDX
                   #fenetre calcul flux dans arbre NS
                   param_int[1][23] = imin
                   param_int[1][24] = imax
                   param_int[1][25] = jmin
                   param_int[1][26] = jmax
                   param_int[1][27] = kmin
                   param_int[1][28] = kmax
                   #adresse stockage flu:  adr = 1 + (i-i0) +(j-j0)*Ni0 + (k-k0)*Ni0*Nj0
                   param_int[1][29] = imin                             #i0
                   param_int[1][30] = jmin                             #j0
                   param_int[1][31] = kmin                             #k0
                   param_int[1][32] = imax-imin+1                      #Ni0
                   param_int[1][33] = param_int[1][32]*(jmax-jmin+1)   #Ni0*Nj0
                   #no de la zone pour recuperer pointer zone
                   param_int[1][34] = no_z               
                   param_int[1][35] = idir+1               
                   #param_int[1][36] = ndf
                      
                   #print 'dim1=',imin,imax,jmin,jmax,kmin,kmax
                   #print 'idir=',idir+1

                   b[0][2].append(zp)

              no_z +=1
              ndf  +=1

              if(compact==1): _compact(zp, fields=var+vargrad)

    Internal._rmNodesByType(b,'ZoneGridConnectivity_t')
    Internal._rmNodesByType(b,'ZoneBC_t')
    Internal._rmNodesByType(b,'Rind_t')
    Internal._rmNodesByName(b,'.Solver#define')
    Internal._rmNodesByName(b,'Parameter_real')
    Internal._rmNodesByName(b,'CFL_minmaxmoy')
    Internal._rmNodesByName(b,'type_zone')
    Internal._rmNodesByName(b,'model')
    Internal._rmNodesByName(b,'temporal_scheme')
    Internal._rmNodesByName(b,'GridCoordinates#Init')
    Internal._rmNodesByName(b,'FlowSolution')

    return teff

#==============================================================================
#
# Calcul des effort (in place)
#
#==============================================================================
def _computeStress(t, teff, metrics):
    global FIRST_IT, HOOK
    zones     = Internal.getZones(t)
    zones_eff = Internal.getZones(teff)
    
    
    # Cree des tableaux temporaires de travail (wiggle, coe, drodm, lok, iskip_lu)
    if HOOK is None: 
            dtloc  = Internal.getNodeFromName3(t, '.Solver#dtloc')  # noeud
            dtloc  = Internal.getValue(dtloc)                       # tab numpy
            HOOK   = FastI.createWorkArrays__(zones, dtloc, FIRST_IT); 
            nitrun =0; nstep =1;
    	    hook1  = HOOK + fasts.souszones_list(zones, metrics, HOOK, nitrun, nstep)
    else:  hook1  = HOOK

    effort = numpy.empty(8, numpy.float64)

    fasts.compute_effort(zones, zones_eff, metrics, hook1, effort)

    return effort

#==============================================================================
# compute guillaume
#==============================================================================
def _computeguillaume1(t, metrics, nitrun, tc=None):
    global FIRST_IT, HOOK, HOOKIBC
    zones = Internal.getZones(t)
    dtloc = Internal.getNodeFromName3(t, '.Solver#dtloc')  # noeud
    dtloc = Internal.getValue(dtloc)                       # tab numpy
    nitmax = int(dtloc[0])
   
    #print 'nitmax= ',nitmax

    rostk = numpy.empty(100000, dtype=numpy.float64)
    drodmstk = numpy.empty(100000, dtype=numpy.float64)
    
    # Cree des tableaux temporaires de travail (wiggle, coe, drodm, lok, iskip_lu)
    if HOOK is None: HOOK = FastI.createWorkArrays__(zones, dtloc, FIRST_IT)
    # IBC 
    if HOOKIBC is None: HOOKIBC = FastI.getIBCInfo__(t)
    bcType = HOOKIBC[0]
    Gamma=HOOKIBC[1]; Cv=HOOKIBC[2]; Mus=HOOKIBC[3];Cs=HOOKIBC[4]; Ts=HOOKIBC[5]

    rostk = numpy.empty(100000, dtype=numpy.float64)
    drodmstk = numpy.empty(100000, dtype=numpy.float64)
    
    for nstep in xrange(1, nitmax+1): # Etape explicit local
             
	# determination taille des zones a integrer (implicit ou explicit local)
    	hook1 = HOOK + fasts.souszones_list(zones, metrics, HOOK, nitrun, nstep)#+ WorkArraysexploc__(t,zones)
	nidom_loc = hook1[11]
        
        if (hook1[13] == 0 and nstep == nitmax and nitmax > nitmax ):nidom_loc = 0          
          
        #print 'nstep' , nstep,nidom_loc,nitmax
                  
        if nidom_loc > 0:
              fasts._computePT(zones, metrics, nitrun, nstep, hook1)
        
        #fonction qui controle stockage, destockage et calcule le predicteur sans passer par le code entier
        fasts.stockrecup(zones,rostk,drodmstk,hook1,nstep)
               
        
        # apply transfers
        if tc is not None:
                # hook1[10] = nombre equations
                if   (hook1[10] == 5 and nstep%2 == 1): vars = varsP; varType = 2                                       
                elif (hook1[10] == 5 and nstep%2 == 0): vars = varsN; varType = 2                    
                elif (hook1[10] == 6 and nstep == 2)  : vars = varsN_SA; varType = 21
                else                                  : vars = varsP_SA; varType = 21
                for v in vars: C._cpVars(t, 'centers:'+v, tc, v)                            
                     
                # transferts IBC en premier
                X._setInterpTransfers(t, tc, variables=None,
                                      variablesIBC=vars, bcType=bcType,
                                      varType=varType, storage=1,
                                      Gamma=Gamma, Cv=Cv, MuS=Mus, 
                                      Cs=Cs, Ts=Ts)   

                # transferts chimere ensuite
                X._setInterpTransfers(t, tc, variables=vars,
                                      variablesIBC=None, bcType=bcType,
                                      varType=varType, storage=1)
                
                           
                                                                
                           
    # switch pointers
    switchPointers__(zones,2)
    # flag pour derivee temporelle 1er pas de temps implicit
    HOOK[9]  = 1
    FIRST_IT = 1
    return None

#==============================================================================
# compute_dpJ_dpW in place
# graph is a dummy argument to be compatible with mpi version
#==============================================================================
def _compute_dpJ_dpW(t, teff, metrics, cosAoA, sinAoA, surfinv):

    global FIRST_IT, HOOK

    zones     = Internal.getZones(t)
    zones_eff = Internal.getZones(teff)

    if HOOK is None: 
            dtloc  = Internal.getNodeFromName3(t, '.Solver#dtloc')  # noeud
            dtloc  = Internal.getValue(dtloc)                       # tab numpy
            HOOK   = FastI.createWorkArrays__(zones, dtloc, FIRST_IT); 
            nitrun =0; nstep =1;
    	    hook1  = HOOK + fasts.souszones_list(zones, metrics, HOOK, nitrun, nstep)
    else:   hook1  = HOOK

    fasts.compute_dpJ_dpW(zones, zones_eff, metrics, hook1, cosAoA, sinAoA, surfinv)
    return None


#==============================================================================
# computeAdjoint in place
# graph is a dummy argument to be compatible with mpi version
#==============================================================================
def _computeAdjoint(t, metrics, nit_adjoint, indFunc, tc=None, graph=None):

    global FIRST_IT, HOOK, HOOKIBC

    bases  = Internal.getNodesFromType1(t     , 'CGNSBase_t')       # noeud
    own   = Internal.getNodeFromName1(bases[0], '.Solver#ownData')  # noeud
    dtloc = Internal.getNodeFromName1(own     , '.Solver#dtloc')    # noeud

    zones = []
    for f in bases:
        zones += Internal.getNodesFromType1(f, 'Zone_t') 

    node = Internal.getNodeFromName1(bases[0], '.Solver#define')
    node = Internal.getNodeFromName1(node, 'omp_mode')
    omp_mode = 0
    if  node is not None: omp_mode = Internal.getValue(node)

    dtloc = Internal.getValue(dtloc) # tab numpy
    nitmax = int(dtloc[0])                 
    orderRk = int(dtloc[len(dtloc)-1])
    
    bcType = HOOKIBC[0]; Gamma=HOOKIBC[1]; Cv=HOOKIBC[2]; Mus=HOOKIBC[3]; Cs=HOOKIBC[4]; Ts=HOOKIBC[5]


    if tc is not None:
         bases = Internal.getNodesFromType1(tc, 'CGNSBase_t')  # noeud
         tc_compact = Internal.getNodeFromName1(bases[0], 'Parameter_real')
         if tc_compact is not None:
                param_real= tc_compact[1]
                param_int = Internal.getNodeFromName1( bases[0], 'Parameter_int' )[1]
                
                zonesD = []
                for f in bases:
                    tmp = Internal.getNodesFromType1(f, 'Zone_t') 
                    zonesD += tmp

    nstep=1
    hook1  = HOOK + fasts.souszones_list(zones, metrics, HOOK, nit_adjoint, nstep)
#    hook1  = HOOK
    


   #--------------------------------------------------------------------------
   #  0  peut-on faire ici un test pour verifier que dpJdpW a bien ete prealablement
   #   calcule (ordre correct des appelants) (boolean isdpJpWComputed ... ?) IVAN
   # champ adjoint et increment adjoint initialise a 0 (?) a verifier
   #--------------------------------------------------------------------------------

  # if nit_adjoint == 1 :
       # if indFunc == 1 :
       #      vars = ['dpCLp_dpDensity']
       #      Connector.connector.___setInterpTransfers(zones, zonesD, vars, param_int, param_real, varType, bcType, type_transfert, no_transfert,Gamma,Cv,Mus,Cs,Ts)

       # if indFunc == 2 : 
       #      vars = ['dpCDp_dpDensity']
       #      Connector.connector.___setInterpTransfers(zones, zonesD, vars, param_int, param_real, varType, bcType, type_transfert, no_transfert,Gamma,Cv,Mus,Cs,Ts)

       # if vars is not None:
            # compute_dpJ_dpW = True       #dpJ_dpW is already computed
            # print "dpJ_dpW is already computed"
       # else:
            # print "dpJ_dpW is missing"   #dpJ_dpW is not computed
            # return None                  #This is equivalent to exit(), because dpJ_dpW has not been computed, so we can not compute RhsIter_Adjoint.

   #--------------------------------------------------------------------------------
   #  1 raccord sur l'adjoint via les ghost cells
   #-----------------------------------

#    if indFunc == 1:
#        vars = ['AdjCLp_RDensity']     # should work for the five variables of incAdj (?)
#           
#    if indFunc == 2: 
#        vars = ['AdjCDp_RDensity']     # should work for the five variables of incAdj (?)
#
#    # apply transfers
#    if tc is not None and hook1[12] ==0: 
#       if   hook1[10] == 5: varType = 2
#       else               : varType = 21

#    #print 'transfert', nstep, skip,hook1[13], hook1[12]        
#    if tc_compact is not None:
#       for v in vars: C._cpVars(t, 'centers:'+v, tc, v)
#       type_transfert = 2  # 0= ID uniquement, 1= IBC uniquement, 2= All
#       no_transfert   = 1  # dans la list des transfert point a point

#    Connector.connector.___setInterpTransfers(zones, zonesD, vars, param_int, param_real, varType, bcType, type_transfert, no_transfert,Gamma,Cv,Mus,Cs,Ts)


#    #-----------------------------------------------------------------
#    #  2 calcul membres de droite et de gauche de l'algo iteratif pour l'adjoint 
#    #--------------------------------------------

#    fasts.compute_RhsIterAdjoint(zones, metrics, nit_adjoint, nstep, indFunc, omp_mode, hook1)

#    return None
 

    #-----------------------------------------------------------------
    #  3 calcul membres de droite de l'algo iteratif pour l'adjoint 
    #--------------------------------------------
    
''' 
     vars = ['IncAdj']     

     do i=1,6 
  
       fasts.compute_LorUAdjoint(zones, metrics, nitrun, nstep, indFunc, omp_mode, hook1)

       Connector.connector.___setInterpTransfers(zones, zonesD, vars, param_int, param_real, varType, bcType, type_transfert, no_transfert,Gamma,Cv,Mus,Cs,Ts)
       enddo 
    #-----------------------------------------------------------------
    #  4 update
    #------------------------------------------------------
 
    if indFunc == 1:
        # adjCLp = adjCLp + incAdj
    if indFunc == 2: 
        # adjCDp = adjCDp + incAdj
'''

   # return None

'''
#==============================================================================
# computedJdX in place
# graph is a dummy argument to be compatible with mpi version
#==============================================================================
def _computedJdX(t, metrics, nitrun, tc=None, graph=None, indFunc):
    global FIRST_IT, HOOK, HOOKIBC

    bases  = Internal.getNodesFromType1(t     , 'CGNSBase_t')       # noeud
    own   = Internal.getNodeFromName1(bases[0], '.Solver#ownData')  # noeud
    dtloc = Internal.getNodeFromName1(own     , '.Solver#dtloc')    # noeud

    zones = []
    for f in bases:
        zones += Internal.getNodesFromType1(f, 'Zone_t') 

    node = Internal.getNodeFromName1(bases[0], '.Solver#define')
    node = Internal.getNodeFromName1(node, 'omp_mode')
    omp_mode = 0
    if  node is not None: omp_mode = Internal.getValue(node)

    dtloc = Internal.getValue(dtloc) # tab numpy
    nitmax = int(dtloc[0])                 
    orderRk = int(dtloc[len(dtloc)-1])
    
    bcType = HOOKIBC[0]; Gamma=HOOKIBC[1]; Cv=HOOKIBC[2]; Mus=HOOKIBC[3]; Cs=HOOKIBC[4]; Ts=HOOKIBC[5]

    if tc is not None:
         bases = Internal.getNodesFromType1(tc, 'CGNSBase_t')  # noeud
         tc_compact = Internal.getNodeFromName1(bases[0], 'Parameter_real')
         if tc_compact is not None:
                param_real= tc_compact[1]
                param_int = Internal.getNodeFromName1( bases[0], 'Parameter_int' )[1]
                
                zonesD = []
                for f in bases:
                    tmp = Internal.getNodesFromType1(f, 'Zone_t') 
                    zonesD += tmp

   #--------------------------------------------------------------------------------

    fasts._computedJdX(zones, metrics, omp_mode, hook1, indFunc)

    return None

'''

