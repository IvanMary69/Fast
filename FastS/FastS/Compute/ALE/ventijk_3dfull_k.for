            lx1 = l111 + incj       
            lx2 = l111 + inci        
            lx3 = lx1  + inci 

#include "FastS/Compute/ALE/target_pt.for"

            cax =  rot(1,1)*(cenx - param_real(ROT_CENTER  ))
     &           + rot(1,2)*(ceny - param_real(ROT_CENTER+1))
     &           + rot(1,3)*(cenz - param_real(ROT_CENTER+2))
 
            cay =  rot(2,1)*(cenx - param_real(ROT_CENTER  ))
     &           + rot(2,2)*(ceny - param_real(ROT_CENTER+1))
     &           + rot(2,3)*(cenz - param_real(ROT_CENTER+2))

            caz =  rot(3,1)*(cenx - param_real(ROT_CENTER  ))
     &           + rot(3,2)*(ceny - param_real(ROT_CENTER+1))
     &           + rot(3,3)*(cenz - param_real(ROT_CENTER+2))

            ventk(l)  = vtrans(1) + rot(4,2)*caz - rot(4,3)*cay
            ventk(l2) = vtrans(2) + rot(4,3)*cax - rot(4,1)*caz
            ventk(l3) = vtrans(3) + rot(4,1)*cay - rot(4,2)*cax
