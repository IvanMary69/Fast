          m =indmy( i, j, 1)
          l =inddm( i, j, 1)

          !cn prise en compte Nb echantillon espace et temps
          rho        = rop(l,1)
          rho_cn     = rop(l,1) * cn

          rho_1      = 1. / rho

          rou_cn     = rop(l,2)*rho_cn
          rov_cn     = rop(l,3)*rho_cn
          u1         = rop(l,2)
          u2         = rop(l,3)

          p          = rop(l,5)*rho*rg
          p_cn       = p* cn

          mu         =  xmut(l)
          mu_cn      =  xmut(l) * cn

          moy (m, 1) = moy(m, 1) + rou_cn  
          moy (m, 2) = moy(m, 2) + rov_cn  
          moy (m, 4) = moy(m, 4) + rho_cn  
          moy (m, 5) = moy(m, 5) + p_cn  
          moy (m, 6) = moy(m, 6) + p_cn * p
          moy (m, 7) = moy(m, 7) + mu_cn
          moy (m, 8) = moy(m, 8) + rou_cn * u1 
          moy (m, 9) = moy(m, 9) + rov_cn * u2 
          moy (m,11) = moy(m,11) + rou_cn * u2 

