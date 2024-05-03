c***********************************************************************
c     $Date: 2010-01-28 16:22:02 +0100 (Thu, 28 Jan 2010) 
c     $ $Revision: 56 $ 
c     $Author: IvanMary $
c*****a*****************************************************************
      subroutine conservrk3local(idir,param_int,ind_loop,drodm,coe,
     & constk,pos,taille,nstep,ind)
    
c***********************************************************************
c_U   USER : PECHIER
c
c     ACT
c_A    extrapolation ordere zero cell fictive
c
c     VAL
c_V    Optimisation NEC
c
c     COM
c***********************************************************************
      implicit none

#include "FastS/param_solver.h"

      INTEGER_E idir, ind_loop(6),param_int(0:*),pos,taille,nstep,ind

      REAL_E drodm(param_int(NDIMDX),param_int(NEQ))
      REAL_E coe(param_int(NDIMDX),param_int(NEQ_COE))
      REAL_E constk(taille/param_int(NEQ),param_int(NEQ))
            
C Var local
      INTEGER_E l,ijkm,im,jm,km,ldjr,i,j,k,ne,lij,neq,n,nistk,lstk
      INTEGER_E nistk2,nistk3,cycl
      REAL_E coeff1, coeff2,c1
     
  
#include "FastS/formule_param.h"                          

       
      cycl = param_int(NSSITER)/param_int(LEVEL)
      neq=param_int(NEQ)


       if (mod(nstep,cycl)==1 .and. ind ==2) then
          coeff2=0.5
       else if (mod(nstep,cycl)==1 .and. ind ==1) then
          coeff1=1.
          !coeff2=0.21132486540518713
          coeff2=0.5
       else if (mod(nstep,cycl)==cycl/2) then
          coeff1=1.
          !coeff2=0.42264973081037416
          coeff2=0.9106836025229591
       else if (mod(nstep,cycl)==cycl-1) then
          coeff1=1.
          coeff2=0.3660254037844387
       end if

       
       nistk = (ind_loop(2)- ind_loop(1))+1
       nistk2 =(ind_loop(4)- ind_loop(3))+1
       nistk3 =(ind_loop(6)- ind_loop(5))+1

       IF (mod(nstep,cycl)==1 .and. ind ==2) then

          do  ne=1,neq
             do  k = ind_loop(5), ind_loop(6)
                do  j = ind_loop(3), ind_loop(4)
                   do  i = ind_loop(1), ind_loop(2)                              
               
           l  = inddm(i,j,k)


           lstk  =  (i+1 - ind_loop(1))
     &              +(j - ind_loop(3))*nistk
     &            +(k-ind_loop(5))*nistk*nistk2
     &         + pos*nistk*nistk2*nistk3

           constk(lstk,ne) = coeff2*drodm(l,ne) 
           
                   end do
                 end do
              end do
           end do
       

       ELSE

 
          do  ne=1,neq
             do  k = ind_loop(5), ind_loop(6)
                do  j = ind_loop(3), ind_loop(4)
                   do  i = ind_loop(1), ind_loop(2)                              
                      
                      l  = inddm(i,j,k)


                      lstk  =  (i+1 - ind_loop(1))
     &                     +(j - ind_loop(3))*nistk
     &                     +(k-ind_loop(5))*nistk*nistk2
     &                     + pos*nistk*nistk2*nistk3

                      constk(lstk,ne) = coeff1*constk(lstk,ne) + 
     &                          !(coe(l,1)/float(param_int(LEVEL)))*coeff2*drodm(l,ne)
     &                     coeff2*drodm(l,ne) 

                      c1=coe(l,1)/float(param_int(LEVEL))


                   end do
                end do
             end do
          end do

       END IF

      end 
     
       
